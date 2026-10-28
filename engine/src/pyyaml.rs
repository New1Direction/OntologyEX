//! A YAML loader with the semantics of `discover_repo.load_yaml`: a direct port
//! of PyYAML 6's pure-Python reader, scanner, and parser (same character
//! classes, line breaks, tab rules, and simple-key logic), followed by the
//! SafeLoader resolver with only `true`/`false` spellings as booleans.
//!
//! It refuses what the Python loader refuses: anchors, aliases, explicit tags,
//! more than 50,000 tokens, nesting beyond 64 open collections, non-printable
//! characters, more than one document, non-string or duplicate mapping keys,
//! merge (`<<`) and value (`=`) scalars, and plain scalars that resolve to an
//! integer or timestamp Python cannot construct (`0x_`, `2024-13-01`).
//!
//! The tree keeps only what discovery reads: strings, mappings in insertion
//! order, sequences, and "some other scalar".

use std::collections::{BTreeMap, VecDeque};

pub const MAX_TOKENS: usize = 50_000;
pub const MAX_NESTING: i64 = 64;

#[derive(Debug, Clone, PartialEq)]
pub enum Yaml {
    Null,
    Bool(bool),
    /// An integer, float, or timestamp: present, but never a string.
    Other,
    Str(String),
    Seq(Vec<Yaml>),
    Map(Vec<(String, Yaml)>),
}

impl Yaml {
    pub fn get(&self, key: &str) -> Option<&Yaml> {
        match self {
            Yaml::Map(entries) => entries.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
}

type Res<T> = Result<T, String>;

fn fail<T>(message: &str) -> Res<T> {
    Err(message.to_string())
}

// ---------------------------------------------------------------- reader

/// PyYAML's reader on bytes: UTF-16 by BOM, otherwise UTF-8 (the BOM stays in
/// the text; the scanner skips it), then the printable check. A NUL ends the
/// buffer.
fn decode(data: &[u8]) -> Res<Vec<char>> {
    let text = if data.starts_with(&[0xFF, 0xFE]) || data.starts_with(&[0xFE, 0xFF]) {
        if data.len() % 2 != 0 {
            return fail("truncated data");
        }
        let little = data[0] == 0xFF;
        let units: Vec<u16> = data
            .chunks_exact(2)
            .map(|p| if little { u16::from_le_bytes([p[0], p[1]]) } else { u16::from_be_bytes([p[0], p[1]]) })
            .collect();
        String::from_utf16(&units).map_err(|_| "invalid UTF-16".to_string())?
    } else {
        String::from_utf8(data.to_vec()).map_err(|_| "invalid UTF-8".to_string())?
    };
    let printable = |c: char| {
        matches!(c, '\t' | '\n' | '\r' | ' '..='~' | '\u{85}' | '\u{A0}'..='\u{D7FF}' | '\u{E000}'..='\u{FFFD}')
            || ('\u{10000}'..='\u{10FFFF}').contains(&c)
    };
    if !text.chars().all(printable) {
        return fail("special characters are not allowed");
    }
    let mut chars: Vec<char> = text.chars().collect();
    chars.push('\0');
    Ok(chars)
}

// ---------------------------------------------------------------- scanner

const BREAKS: &str = "\r\n\u{85}\u{2028}\u{2029}";
const BREAKZ: &str = "\0\r\n\u{85}\u{2028}\u{2029}";
const BLANKZ: &str = "\0 \t\r\n\u{85}\u{2028}\u{2029}";
const SPACEZ: &str = "\0 \r\n\u{85}\u{2028}\u{2029}"; // like BLANKZ without the tab

fn is_in(c: char, set: &str) -> bool {
    set.contains(c)
}

fn word_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '-' || c == '_'
}

#[derive(Debug, Clone, PartialEq)]
pub enum Tok {
    StreamStart,
    StreamEnd,
    /// `%YAML` (with whether the major version is 1), `%TAG` (with its handle), or other.
    Directive { yaml_major_one: Option<bool>, tag_handle: Option<String> },
    DocumentStart,
    DocumentEnd,
    BlockSequenceStart,
    BlockMappingStart,
    BlockEnd,
    FlowSequenceStart,
    FlowSequenceEnd,
    FlowMappingStart,
    FlowMappingEnd,
    BlockEntry,
    FlowEntry,
    Key,
    Value,
    Alias,
    Anchor,
    Tag,
    Scalar { value: String, plain: bool },
}

struct SimpleKey {
    token_number: usize,
    required: bool,
    index: usize,
    line: usize,
    column: usize,
}

struct Scanner {
    buf: Vec<char>,
    pointer: usize,
    line: usize,
    column: usize,
    done: bool,
    flow_level: usize,
    tokens: VecDeque<Tok>,
    tokens_taken: usize,
    indent: i64,
    indents: Vec<i64>,
    allow_simple_key: bool,
    possible_simple_keys: BTreeMap<usize, SimpleKey>,
}

impl Scanner {
    fn new(buf: Vec<char>) -> Self {
        let mut s = Scanner {
            buf, pointer: 0, line: 0, column: 0, done: false, flow_level: 0, tokens: VecDeque::new(),
            tokens_taken: 0, indent: -1, indents: Vec::new(), allow_simple_key: true,
            possible_simple_keys: BTreeMap::new(),
        };
        s.tokens.push_back(Tok::StreamStart);
        s
    }

    fn peek(&self, n: usize) -> char {
        *self.buf.get(self.pointer + n).unwrap_or(&'\0')
    }

    fn prefix(&self, n: usize) -> String {
        let end = (self.pointer + n).min(self.buf.len());
        self.buf[self.pointer..end].iter().collect()
    }

    fn forward(&mut self, n: usize) {
        for _ in 0..n {
            let ch = self.buf[self.pointer];
            self.pointer += 1;
            if is_in(ch, "\n\u{85}\u{2028}\u{2029}") || (ch == '\r' && self.peek(0) != '\n') {
                self.line += 1;
                self.column = 0;
            } else if ch != '\u{FEFF}' {
                self.column += 1;
            }
        }
    }

    /// The next token, or `None` after the stream end.
    fn next_token(&mut self) -> Res<Option<Tok>> {
        while self.need_more_tokens()? {
            self.fetch_more_tokens()?;
        }
        Ok(self.tokens.pop_front().inspect(|_| self.tokens_taken += 1))
    }

    fn need_more_tokens(&mut self) -> Res<bool> {
        if self.done {
            return Ok(false);
        }
        if self.tokens.is_empty() {
            return Ok(true);
        }
        self.stale_possible_simple_keys()?;
        Ok(self.next_possible_simple_key() == Some(self.tokens_taken))
    }

    fn fetch_more_tokens(&mut self) -> Res<()> {
        self.scan_to_next_token();
        self.stale_possible_simple_keys()?;
        self.unwind_indent(self.column as i64);
        let ch = self.peek(0);
        match ch {
            '\0' => return self.fetch_stream_end(),
            '%' if self.column == 0 => return self.fetch_directive(),
            '-' if self.check_document_indicator("---") => return self.fetch_document_indicator(Tok::DocumentStart),
            '.' if self.check_document_indicator("...") => return self.fetch_document_indicator(Tok::DocumentEnd),
            '[' => return self.fetch_flow_collection_start(Tok::FlowSequenceStart),
            '{' => return self.fetch_flow_collection_start(Tok::FlowMappingStart),
            ']' => return self.fetch_flow_collection_end(Tok::FlowSequenceEnd),
            '}' => return self.fetch_flow_collection_end(Tok::FlowMappingEnd),
            ',' => return self.fetch_flow_entry(),
            _ => {}
        }
        let next_blank = is_in(self.peek(1), BLANKZ);
        if ch == '-' && next_blank {
            return self.fetch_block_entry();
        }
        if ch == '?' && (self.flow_level > 0 || next_blank) {
            return self.fetch_key();
        }
        if ch == ':' && (self.flow_level > 0 || next_blank) {
            return self.fetch_value();
        }
        match ch {
            '*' => return self.fetch_anchor_like(Tok::Alias),
            '&' => return self.fetch_anchor_like(Tok::Anchor),
            '!' => return self.fetch_tag(),
            '|' if self.flow_level == 0 => return self.fetch_block_scalar(false),
            '>' if self.flow_level == 0 => return self.fetch_block_scalar(true),
            '\'' => return self.fetch_flow_scalar(false),
            '"' => return self.fetch_flow_scalar(true),
            _ => {}
        }
        if self.check_plain() {
            return self.fetch_plain();
        }
        fail("found character that cannot start any token")
    }

    fn next_possible_simple_key(&self) -> Option<usize> {
        self.possible_simple_keys.values().map(|k| k.token_number).min()
    }

    fn stale_possible_simple_keys(&mut self) -> Res<()> {
        let levels: Vec<usize> = self.possible_simple_keys.keys().copied().collect();
        for level in levels {
            let key = &self.possible_simple_keys[&level];
            if key.line != self.line || self.pointer - key.index > 1024 {
                if key.required {
                    return fail("could not find expected ':'");
                }
                self.possible_simple_keys.remove(&level);
            }
        }
        Ok(())
    }

    fn save_possible_simple_key(&mut self) -> Res<()> {
        let required = self.flow_level == 0 && self.indent == self.column as i64;
        if self.allow_simple_key {
            self.remove_possible_simple_key()?;
            let key = SimpleKey {
                token_number: self.tokens_taken + self.tokens.len(), required,
                index: self.pointer, line: self.line, column: self.column,
            };
            self.possible_simple_keys.insert(self.flow_level, key);
        }
        Ok(())
    }

    fn remove_possible_simple_key(&mut self) -> Res<()> {
        if let Some(key) = self.possible_simple_keys.remove(&self.flow_level) {
            if key.required {
                return fail("could not find expected ':'");
            }
        }
        Ok(())
    }

    fn unwind_indent(&mut self, column: i64) {
        if self.flow_level > 0 {
            return;
        }
        while self.indent > column {
            self.indent = self.indents.pop().unwrap_or(-1);
            self.tokens.push_back(Tok::BlockEnd);
        }
    }

    fn add_indent(&mut self, column: i64) -> bool {
        if self.indent < column {
            self.indents.push(self.indent);
            self.indent = column;
            return true;
        }
        false
    }

    fn fetch_stream_end(&mut self) -> Res<()> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        self.possible_simple_keys.clear();
        self.tokens.push_back(Tok::StreamEnd);
        self.done = true;
        Ok(())
    }

    fn fetch_directive(&mut self) -> Res<()> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_directive()?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn check_document_indicator(&self, mark: &str) -> bool {
        self.column == 0 && self.prefix(3) == mark && is_in(self.peek(3), BLANKZ)
    }

    fn fetch_document_indicator(&mut self, token: Tok) -> Res<()> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        self.forward(3);
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_flow_collection_start(&mut self, token: Tok) -> Res<()> {
        self.save_possible_simple_key()?;
        self.flow_level += 1;
        self.allow_simple_key = true;
        self.forward(1);
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_flow_collection_end(&mut self, token: Tok) -> Res<()> {
        self.remove_possible_simple_key()?;
        // Python lets flow_level go negative on a stray closer; the parser then fails.
        self.flow_level = self.flow_level.wrapping_sub(1);
        if self.flow_level == usize::MAX {
            return fail("unexpected flow collection end");
        }
        self.allow_simple_key = false;
        self.forward(1);
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_flow_entry(&mut self) -> Res<()> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        self.forward(1);
        self.tokens.push_back(Tok::FlowEntry);
        Ok(())
    }

    fn fetch_block_entry(&mut self) -> Res<()> {
        if self.flow_level == 0 {
            if !self.allow_simple_key {
                return fail("sequence entries are not allowed here");
            }
            if self.add_indent(self.column as i64) {
                self.tokens.push_back(Tok::BlockSequenceStart);
            }
        }
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        self.forward(1);
        self.tokens.push_back(Tok::BlockEntry);
        Ok(())
    }

    fn fetch_key(&mut self) -> Res<()> {
        if self.flow_level == 0 {
            if !self.allow_simple_key {
                return fail("mapping keys are not allowed here");
            }
            if self.add_indent(self.column as i64) {
                self.tokens.push_back(Tok::BlockMappingStart);
            }
        }
        self.allow_simple_key = self.flow_level == 0;
        self.remove_possible_simple_key()?;
        self.forward(1);
        self.tokens.push_back(Tok::Key);
        Ok(())
    }

    fn fetch_value(&mut self) -> Res<()> {
        if let Some(key) = self.possible_simple_keys.remove(&self.flow_level) {
            let at = key.token_number - self.tokens_taken;
            self.tokens.insert(at, Tok::Key);
            if self.flow_level == 0 && self.add_indent(key.column as i64) {
                self.tokens.insert(at, Tok::BlockMappingStart);
            }
            self.allow_simple_key = false;
        } else {
            if self.flow_level == 0 {
                if !self.allow_simple_key {
                    return fail("mapping values are not allowed here");
                }
                if self.add_indent(self.column as i64) {
                    self.tokens.push_back(Tok::BlockMappingStart);
                }
            }
            self.allow_simple_key = self.flow_level == 0;
            self.remove_possible_simple_key()?;
        }
        self.forward(1);
        self.tokens.push_back(Tok::Value);
        Ok(())
    }

    fn fetch_anchor_like(&mut self, token: Tok) -> Res<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        self.forward(1);
        let mut length = 0;
        while word_char(self.peek(length)) {
            length += 1;
        }
        if length == 0 {
            return fail("expected alphabetic or numeric character");
        }
        self.forward(length);
        if !is_in(self.peek(0), "\0 \t\r\n\u{85}\u{2028}\u{2029}?:,]}%@`") {
            return fail("expected alphabetic or numeric character");
        }
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_tag(&mut self) -> Res<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let ch = self.peek(1);
        if ch == '<' {
            self.forward(2);
            self.scan_tag_uri()?;
            if self.peek(0) != '>' {
                return fail("expected '>'");
            }
            self.forward(1);
        } else if is_in(ch, BLANKZ) {
            self.forward(1);
        } else {
            let mut length = 1;
            let mut use_handle = false;
            let mut c = ch;
            while !is_in(c, SPACEZ) {
                if c == '!' {
                    use_handle = true;
                    break;
                }
                length += 1;
                c = self.peek(length);
            }
            if use_handle {
                self.scan_tag_handle()?;
            } else {
                self.forward(1);
            }
            self.scan_tag_uri()?;
        }
        if !is_in(self.peek(0), SPACEZ) {
            return fail("expected ' '");
        }
        self.tokens.push_back(Tok::Tag);
        Ok(())
    }

    fn fetch_block_scalar(&mut self, folded: bool) -> Res<()> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        let token = self.scan_block_scalar(folded)?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_flow_scalar(&mut self, double: bool) -> Res<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_flow_scalar(double)?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_plain(&mut self) -> Res<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_plain();
        self.tokens.push_back(token);
        Ok(())
    }

    fn check_plain(&self) -> bool {
        let ch = self.peek(0);
        !is_in(ch, "\0 \t\r\n\u{85}\u{2028}\u{2029}-?:,[]{}#&*!|>'\"%@`")
            || (!is_in(self.peek(1), BLANKZ) && (ch == '-' || (self.flow_level == 0 && is_in(ch, "?:"))))
    }

    fn scan_to_next_token(&mut self) {
        if self.pointer == 0 && self.peek(0) == '\u{FEFF}' {
            self.forward(1);
        }
        loop {
            while self.peek(0) == ' ' {
                self.forward(1);
            }
            if self.peek(0) == '#' {
                while !is_in(self.peek(0), BREAKZ) {
                    self.forward(1);
                }
            }
            if !self.scan_line_break().is_empty() {
                if self.flow_level == 0 {
                    self.allow_simple_key = true;
                }
            } else {
                break;
            }
        }
    }

    fn scan_directive(&mut self) -> Res<Tok> {
        self.forward(1);
        let mut length = 0;
        while word_char(self.peek(length)) {
            length += 1;
        }
        if length == 0 {
            return fail("expected alphabetic or numeric character");
        }
        let name = self.prefix(length);
        self.forward(length);
        if !is_in(self.peek(0), SPACEZ) {
            return fail("expected alphabetic or numeric character");
        }
        let mut token = Tok::Directive { yaml_major_one: None, tag_handle: None };
        if name == "YAML" {
            while self.peek(0) == ' ' {
                self.forward(1);
            }
            let major = self.scan_directive_number()?;
            if self.peek(0) != '.' {
                return fail("expected a digit or '.'");
            }
            self.forward(1);
            self.scan_directive_number()?;
            if !is_in(self.peek(0), SPACEZ) {
                return fail("expected a digit or ' '");
            }
            token = Tok::Directive { yaml_major_one: Some(major.trim_start_matches('0') == "1"), tag_handle: None };
        } else if name == "TAG" {
            while self.peek(0) == ' ' {
                self.forward(1);
            }
            let handle = self.scan_tag_handle()?;
            if self.peek(0) != ' ' {
                return fail("expected ' '");
            }
            while self.peek(0) == ' ' {
                self.forward(1);
            }
            self.scan_tag_uri()?;
            if !is_in(self.peek(0), SPACEZ) {
                return fail("expected ' '");
            }
            token = Tok::Directive { yaml_major_one: None, tag_handle: Some(handle) };
        } else {
            while !is_in(self.peek(0), BREAKZ) {
                self.forward(1);
            }
        }
        while self.peek(0) == ' ' {
            self.forward(1);
        }
        if self.peek(0) == '#' {
            while !is_in(self.peek(0), BREAKZ) {
                self.forward(1);
            }
        }
        if !is_in(self.peek(0), BREAKZ) {
            return fail("expected a comment or a line break");
        }
        self.scan_line_break();
        Ok(token)
    }

    fn scan_directive_number(&mut self) -> Res<String> {
        if !self.peek(0).is_ascii_digit() {
            return fail("expected a digit");
        }
        let mut length = 0;
        while self.peek(length).is_ascii_digit() {
            length += 1;
        }
        let value = self.prefix(length);
        self.forward(length);
        Ok(value)
    }

    fn scan_block_scalar(&mut self, folded: bool) -> Res<Tok> {
        let mut chunks = String::new();
        self.forward(1);
        // Indicators: chomping (+/-) and an indentation increment (1-9), in either order.
        let mut chomping: Option<bool> = None;
        let mut increment: Option<i64> = None;
        let ch = self.peek(0);
        if is_in(ch, "+-") {
            chomping = Some(ch == '+');
            self.forward(1);
            let ch = self.peek(0);
            if ch.is_ascii_digit() {
                if ch == '0' {
                    return fail("expected indentation indicator in the range 1-9, but found 0");
                }
                increment = Some(ch as i64 - '0' as i64);
                self.forward(1);
            }
        } else if ch.is_ascii_digit() {
            if ch == '0' {
                return fail("expected indentation indicator in the range 1-9, but found 0");
            }
            increment = Some(ch as i64 - '0' as i64);
            self.forward(1);
            let ch = self.peek(0);
            if is_in(ch, "+-") {
                chomping = Some(ch == '+');
                self.forward(1);
            }
        }
        if !is_in(self.peek(0), SPACEZ) {
            return fail("expected chomping or indentation indicators");
        }
        // The rest of the indicator line may only hold a comment.
        while self.peek(0) == ' ' {
            self.forward(1);
        }
        if self.peek(0) == '#' {
            while !is_in(self.peek(0), BREAKZ) {
                self.forward(1);
            }
        }
        if !is_in(self.peek(0), BREAKZ) {
            return fail("expected a comment or a line break");
        }
        self.scan_line_break();
        let min_indent = (self.indent + 1).max(1);
        let (mut breaks, indent) = match increment {
            None => {
                let (breaks, max_indent) = self.scan_block_scalar_indentation();
                (breaks, min_indent.max(max_indent))
            }
            Some(inc) => {
                let indent = min_indent + inc - 1;
                (self.scan_block_scalar_breaks(indent), indent)
            }
        };
        let mut line_break = String::new();
        while self.column as i64 == indent && self.peek(0) != '\0' {
            chunks.extend(breaks.iter().map(String::as_str));
            let leading_non_space = !is_in(self.peek(0), " \t");
            let mut length = 0;
            while !is_in(self.peek(length), BREAKZ) {
                length += 1;
            }
            chunks.push_str(&self.prefix(length));
            self.forward(length);
            line_break = self.scan_line_break();
            breaks = self.scan_block_scalar_breaks(indent);
            if self.column as i64 == indent && self.peek(0) != '\0' {
                if folded && line_break == "\n" && leading_non_space && !is_in(self.peek(0), " \t") {
                    if breaks.is_empty() {
                        chunks.push(' ');
                    }
                } else {
                    chunks.push_str(&line_break);
                }
            } else {
                break;
            }
        }
        if chomping != Some(false) {
            chunks.push_str(&line_break);
        }
        if chomping == Some(true) {
            chunks.extend(breaks.iter().map(String::as_str));
        }
        Ok(Tok::Scalar { value: chunks, plain: false })
    }

    fn scan_block_scalar_indentation(&mut self) -> (Vec<String>, i64) {
        let mut chunks = Vec::new();
        let mut max_indent = 0;
        while is_in(self.peek(0), " \r\n\u{85}\u{2028}\u{2029}") {
            if self.peek(0) != ' ' {
                chunks.push(self.scan_line_break());
            } else {
                self.forward(1);
                max_indent = max_indent.max(self.column as i64);
            }
        }
        (chunks, max_indent)
    }

    fn scan_block_scalar_breaks(&mut self, indent: i64) -> Vec<String> {
        let mut chunks = Vec::new();
        while (self.column as i64) < indent && self.peek(0) == ' ' {
            self.forward(1);
        }
        while is_in(self.peek(0), BREAKS) {
            chunks.push(self.scan_line_break());
            while (self.column as i64) < indent && self.peek(0) == ' ' {
                self.forward(1);
            }
        }
        chunks
    }

    fn scan_flow_scalar(&mut self, double: bool) -> Res<Tok> {
        let mut chunks = String::new();
        let quote = self.peek(0);
        self.forward(1);
        self.scan_flow_scalar_non_spaces(double, &mut chunks)?;
        while self.peek(0) != quote {
            self.scan_flow_scalar_spaces(&mut chunks)?;
            self.scan_flow_scalar_non_spaces(double, &mut chunks)?;
        }
        self.forward(1);
        Ok(Tok::Scalar { value: chunks, plain: false })
    }

    fn scan_flow_scalar_non_spaces(&mut self, double: bool, chunks: &mut String) -> Res<()> {
        loop {
            let mut length = 0;
            while !is_in(self.peek(length), "'\"\\\0 \t\r\n\u{85}\u{2028}\u{2029}") {
                length += 1;
            }
            if length > 0 {
                chunks.push_str(&self.prefix(length));
                self.forward(length);
            }
            let ch = self.peek(0);
            if !double && ch == '\'' && self.peek(1) == '\'' {
                chunks.push('\'');
                self.forward(2);
            } else if (double && ch == '\'') || (!double && is_in(ch, "\"\\")) {
                chunks.push(ch);
                self.forward(1);
            } else if double && ch == '\\' {
                self.forward(1);
                let ch = self.peek(0);
                let replacement = match ch {
                    '0' => Some('\0'), 'a' => Some('\u{07}'), 'b' => Some('\u{08}'), 't' | '\t' => Some('\t'),
                    'n' => Some('\n'), 'v' => Some('\u{0B}'), 'f' => Some('\u{0C}'), 'r' => Some('\r'),
                    'e' => Some('\u{1B}'), ' ' => Some(' '), '"' => Some('"'), '\\' => Some('\\'),
                    '/' => Some('/'), 'N' => Some('\u{85}'), '_' => Some('\u{A0}'), 'L' => Some('\u{2028}'),
                    'P' => Some('\u{2029}'), _ => None,
                };
                if let Some(r) = replacement {
                    chunks.push(r);
                    self.forward(1);
                } else if let Some(width) = match ch { 'x' => Some(2), 'u' => Some(4), 'U' => Some(8), _ => None } {
                    self.forward(1);
                    for k in 0..width {
                        if !self.peek(k).is_ascii_hexdigit() {
                            return fail("expected escape sequence of hexadecimal numbers");
                        }
                    }
                    let code = u32::from_str_radix(&self.prefix(width), 16).unwrap_or(u32::MAX);
                    // Python's chr() accepts lone surrogates; this tree cannot hold them,
                    // and no surrogate can be part of a valid key or name anyway.
                    let c = char::from_u32(code)
                        .or_else(|| (0xD800..=0xDFFF).contains(&code).then_some('\u{FFFD}'))
                        .ok_or("chr() arg not in range(0x110000)")?;
                    chunks.push(c);
                    self.forward(width);
                } else if is_in(ch, BREAKS) {
                    self.scan_line_break();
                    self.scan_flow_scalar_breaks(chunks)?;
                } else {
                    return fail("found unknown escape character");
                }
            } else {
                return Ok(());
            }
        }
    }

    fn scan_flow_scalar_spaces(&mut self, chunks: &mut String) -> Res<()> {
        let mut length = 0;
        while is_in(self.peek(length), " \t") {
            length += 1;
        }
        let whitespaces = self.prefix(length);
        self.forward(length);
        let ch = self.peek(0);
        if ch == '\0' {
            return fail("found unexpected end of stream");
        } else if is_in(ch, BREAKS) {
            let line_break = self.scan_line_break();
            let mut breaks = String::new();
            let any = self.scan_flow_scalar_breaks(&mut breaks)?;
            if line_break != "\n" {
                chunks.push_str(&line_break);
            } else if !any {
                chunks.push(' ');
            }
            chunks.push_str(&breaks);
        } else {
            chunks.push_str(&whitespaces);
        }
        Ok(())
    }

    /// Appends line breaks; returns whether there were any.
    fn scan_flow_scalar_breaks(&mut self, chunks: &mut String) -> Res<bool> {
        let mut any = false;
        loop {
            let prefix = self.prefix(3);
            if (prefix == "---" || prefix == "...") && is_in(self.peek(3), BLANKZ) {
                return fail("found unexpected document separator");
            }
            while is_in(self.peek(0), " \t") {
                self.forward(1);
            }
            if is_in(self.peek(0), BREAKS) {
                chunks.push_str(&self.scan_line_break());
                any = true;
            } else {
                return Ok(any);
            }
        }
    }

    fn scan_plain(&mut self) -> Tok {
        let mut chunks = String::new();
        let indent = self.indent + 1;
        let mut spaces: Option<String> = Some(String::new());
        loop {
            let mut length = 0;
            if self.peek(0) == '#' {
                break;
            }
            loop {
                let ch = self.peek(length);
                let after = self.peek(length + 1);
                let ends_value = ch == ':' && (is_in(after, BLANKZ) || (self.flow_level > 0 && is_in(after, ",[]{}")));
                if is_in(ch, BLANKZ) || ends_value || (self.flow_level > 0 && is_in(ch, ",?[]{}")) {
                    break;
                }
                length += 1;
            }
            if length == 0 {
                break;
            }
            self.allow_simple_key = false;
            chunks.push_str(spaces.as_deref().unwrap_or(""));
            chunks.push_str(&self.prefix(length));
            self.forward(length);
            spaces = self.scan_plain_spaces();
            let stop = match &spaces {
                None => true,
                Some(s) => s.is_empty(),
            };
            if stop || self.peek(0) == '#' || (self.flow_level == 0 && (self.column as i64) < indent) {
                break;
            }
        }
        Tok::Scalar { value: chunks, plain: true }
    }

    /// `None` when a document separator follows (Python returns None there).
    fn scan_plain_spaces(&mut self) -> Option<String> {
        let mut chunks = String::new();
        let mut length = 0;
        while self.peek(length) == ' ' {
            length += 1;
        }
        let whitespaces = self.prefix(length);
        self.forward(length);
        let ch = self.peek(0);
        let separator = |s: &Self| {
            let prefix = s.prefix(3);
            (prefix == "---" || prefix == "...") && is_in(s.peek(3), BLANKZ)
        };
        if is_in(ch, BREAKS) {
            let line_break = self.scan_line_break();
            self.allow_simple_key = true;
            if separator(self) {
                return None;
            }
            let mut breaks = String::new();
            let mut any = false;
            while is_in(self.peek(0), " \r\n\u{85}\u{2028}\u{2029}") {
                if self.peek(0) == ' ' {
                    self.forward(1);
                } else {
                    breaks.push_str(&self.scan_line_break());
                    any = true;
                    if separator(self) {
                        return None;
                    }
                }
            }
            if line_break != "\n" {
                chunks.push_str(&line_break);
            } else if !any {
                chunks.push(' ');
            }
            chunks.push_str(&breaks);
        } else if !whitespaces.is_empty() {
            chunks.push_str(&whitespaces);
        }
        Some(chunks)
    }

    fn scan_tag_handle(&mut self) -> Res<String> {
        if self.peek(0) != '!' {
            return fail("expected '!'");
        }
        let mut length = 1;
        let mut ch = self.peek(length);
        if ch != ' ' {
            while word_char(ch) {
                length += 1;
                ch = self.peek(length);
            }
            if ch != '!' {
                self.forward(length);
                return fail("expected '!'");
            }
            length += 1;
        }
        let value = self.prefix(length);
        self.forward(length);
        Ok(value)
    }

    fn scan_tag_uri(&mut self) -> Res<()> {
        let mut any = false;
        let mut length = 0;
        let mut ch = self.peek(length);
        while ch.is_ascii_alphanumeric() || is_in(ch, "-;/?:@&=+$,_.!~*'()[]%") {
            if ch == '%' {
                self.forward(length);
                length = 0;
                // %XX escapes must decode as UTF-8.
                let mut codes = Vec::new();
                while self.peek(0) == '%' {
                    self.forward(1);
                    if !(self.peek(0).is_ascii_hexdigit() && self.peek(1).is_ascii_hexdigit()) {
                        return fail("expected URI escape sequence of 2 hexadecimal numbers");
                    }
                    codes.push(u8::from_str_radix(&self.prefix(2), 16).unwrap());
                    self.forward(2);
                }
                String::from_utf8(codes).map_err(|_| "invalid URI escape".to_string())?;
                any = true;
            } else {
                length += 1;
            }
            ch = self.peek(length);
        }
        if length > 0 {
            self.forward(length);
            any = true;
        }
        if !any {
            return fail("expected URI");
        }
        Ok(())
    }

    fn scan_line_break(&mut self) -> String {
        let ch = self.peek(0);
        if is_in(ch, "\r\n\u{85}") {
            if self.prefix(2) == "\r\n" {
                self.forward(2);
            } else {
                self.forward(1);
            }
            "\n".into()
        } else if is_in(ch, "\u{2028}\u{2029}") {
            self.forward(1);
            ch.to_string()
        } else {
            String::new()
        }
    }
}

/// `yaml.scan` with the limits of `discover_repo.load_yaml`: the whole token stream.
fn scan(chars: Vec<char>) -> Res<Vec<Tok>> {
    let mut scanner = Scanner::new(chars);
    let mut tokens = Vec::new();
    let mut depth: i64 = 0;
    while let Some(token) = scanner.next_token()? {
        if tokens.len() >= MAX_TOKENS || matches!(token, Tok::Anchor | Tok::Alias | Tok::Tag) {
            return fail("YAML exceeds token limit or contains anchors, aliases, or tags");
        }
        depth += match token {
            Tok::BlockSequenceStart | Tok::BlockMappingStart | Tok::FlowSequenceStart | Tok::FlowMappingStart => 1,
            Tok::BlockEnd | Tok::FlowSequenceEnd | Tok::FlowMappingEnd => -1,
            _ => 0,
        };
        if depth > MAX_NESTING {
            return fail("YAML nesting exceeds limit");
        }
        tokens.push(token);
    }
    Ok(tokens)
}

// ---------------------------------------------------------------- parser

#[derive(Debug, Clone, PartialEq)]
enum Event {
    StreamStart,
    StreamEnd,
    DocumentStart,
    DocumentEnd,
    /// value, plain (implicit resolution applies)
    Scalar(String, bool),
    SequenceStart,
    SequenceEnd,
    MappingStart,
    MappingEnd,
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum State {
    StreamStart,
    ImplicitDocumentStart,
    DocumentStart,
    DocumentEnd,
    DocumentContent,
    BlockNode,
    BlockSequenceFirstEntry,
    BlockSequenceEntry,
    IndentlessSequenceEntry,
    BlockMappingFirstKey,
    BlockMappingKey,
    BlockMappingValue,
    FlowSequenceFirstEntry,
    FlowSequenceEntry,
    FlowSequenceEntryMappingKey,
    FlowSequenceEntryMappingValue,
    FlowSequenceEntryMappingEnd,
    FlowMappingFirstKey,
    FlowMappingKey,
    FlowMappingValue,
    FlowMappingEmptyValue,
    Done,
}

struct Parser {
    tokens: Vec<Tok>,
    at: usize,
    state: State,
    states: Vec<State>,
}

impl Parser {
    fn peek(&self) -> &Tok {
        self.tokens.get(self.at).unwrap_or(&Tok::StreamEnd)
    }
    fn is(&self, choices: &[Tok]) -> bool {
        let t = self.peek();
        choices.iter().any(|c| std::mem::discriminant(c) == std::mem::discriminant(t))
    }
    fn take(&mut self) -> Tok {
        let t = self.peek().clone();
        self.at += 1;
        t
    }
    fn pop_state(&mut self) -> State {
        self.states.pop().unwrap_or(State::Done)
    }
    fn empty_scalar(&self) -> Event {
        Event::Scalar(String::new(), true)
    }

    fn next(&mut self) -> Res<Option<Event>> {
        let event = match self.state {
            State::Done => return Ok(None),
            State::StreamStart => {
                self.take();
                self.state = State::ImplicitDocumentStart;
                Event::StreamStart
            }
            State::ImplicitDocumentStart => {
                let explicit = [Tok::Directive { yaml_major_one: None, tag_handle: None }, Tok::DocumentStart, Tok::StreamEnd];
                if !self.is(&explicit) {
                    self.states.push(State::DocumentEnd);
                    self.state = State::BlockNode;
                    Event::DocumentStart
                } else {
                    return self.document_start();
                }
            }
            State::DocumentStart => return self.document_start(),
            State::DocumentEnd => {
                if self.is(&[Tok::DocumentEnd]) {
                    self.take();
                }
                self.state = State::DocumentStart;
                Event::DocumentEnd
            }
            State::DocumentContent => {
                let ends = [Tok::Directive { yaml_major_one: None, tag_handle: None }, Tok::DocumentStart,
                            Tok::DocumentEnd, Tok::StreamEnd];
                if self.is(&ends) {
                    self.state = self.pop_state();
                    self.empty_scalar()
                } else {
                    return self.node(true, false).map(Some);
                }
            }
            State::BlockNode => return self.node(true, false).map(Some),
            State::BlockSequenceFirstEntry => {
                self.take();
                return self.block_sequence_entry();
            }
            State::BlockSequenceEntry => return self.block_sequence_entry(),
            State::IndentlessSequenceEntry => {
                if self.is(&[Tok::BlockEntry]) {
                    self.take();
                    if !self.is(&[Tok::BlockEntry, Tok::Key, Tok::Value, Tok::BlockEnd]) {
                        self.states.push(State::IndentlessSequenceEntry);
                        return self.node(true, false).map(Some);
                    }
                    self.state = State::IndentlessSequenceEntry;
                    self.empty_scalar()
                } else {
                    self.state = self.pop_state();
                    Event::SequenceEnd
                }
            }
            State::BlockMappingFirstKey => {
                self.take();
                return self.block_mapping_key();
            }
            State::BlockMappingKey => return self.block_mapping_key(),
            State::BlockMappingValue => {
                if self.is(&[Tok::Value]) {
                    self.take();
                    if !self.is(&[Tok::Key, Tok::Value, Tok::BlockEnd]) {
                        self.states.push(State::BlockMappingKey);
                        return self.node(true, true).map(Some);
                    }
                }
                self.state = State::BlockMappingKey;
                self.empty_scalar()
            }
            State::FlowSequenceFirstEntry => {
                self.take();
                return self.flow_sequence_entry(true);
            }
            State::FlowSequenceEntry => return self.flow_sequence_entry(false),
            State::FlowSequenceEntryMappingKey => {
                self.take();
                if !self.is(&[Tok::Value, Tok::FlowEntry, Tok::FlowSequenceEnd]) {
                    self.states.push(State::FlowSequenceEntryMappingValue);
                    return self.node(false, false).map(Some);
                }
                self.state = State::FlowSequenceEntryMappingValue;
                self.empty_scalar()
            }
            State::FlowSequenceEntryMappingValue => {
                if self.is(&[Tok::Value]) {
                    self.take();
                    if !self.is(&[Tok::FlowEntry, Tok::FlowSequenceEnd]) {
                        self.states.push(State::FlowSequenceEntryMappingEnd);
                        return self.node(false, false).map(Some);
                    }
                }
                self.state = State::FlowSequenceEntryMappingEnd;
                self.empty_scalar()
            }
            State::FlowSequenceEntryMappingEnd => {
                self.state = State::FlowSequenceEntry;
                Event::MappingEnd
            }
            State::FlowMappingFirstKey => {
                self.take();
                return self.flow_mapping_key(true);
            }
            State::FlowMappingKey => return self.flow_mapping_key(false),
            State::FlowMappingValue => {
                if self.is(&[Tok::Value]) {
                    self.take();
                    if !self.is(&[Tok::FlowEntry, Tok::FlowMappingEnd]) {
                        self.states.push(State::FlowMappingKey);
                        return self.node(false, false).map(Some);
                    }
                }
                self.state = State::FlowMappingKey;
                self.empty_scalar()
            }
            State::FlowMappingEmptyValue => {
                self.state = State::FlowMappingKey;
                self.empty_scalar()
            }
        };
        Ok(Some(event))
    }

    fn document_start(&mut self) -> Res<Option<Event>> {
        while self.is(&[Tok::DocumentEnd]) {
            self.take();
        }
        if self.is(&[Tok::StreamEnd]) {
            self.take();
            self.state = State::Done;
            return Ok(Some(Event::StreamEnd));
        }
        // process_directives
        let mut version_seen = false;
        let mut handles: Vec<String> = Vec::new();
        while let Tok::Directive { yaml_major_one, tag_handle } = self.peek().clone() {
            self.take();
            if let Some(major_one) = yaml_major_one {
                if version_seen {
                    return fail("found duplicate YAML directive");
                }
                if !major_one {
                    return fail("found incompatible YAML document (version 1.* is required)");
                }
                version_seen = true;
            } else if let Some(handle) = tag_handle {
                if handles.contains(&handle) {
                    return fail("duplicate tag handle");
                }
                handles.push(handle);
            }
        }
        if !self.is(&[Tok::DocumentStart]) {
            return fail("expected '<document start>'");
        }
        self.take();
        self.states.push(State::DocumentEnd);
        self.state = State::DocumentContent;
        Ok(Some(Event::DocumentStart))
    }

    fn node(&mut self, block: bool, indentless_sequence: bool) -> Res<Event> {
        if indentless_sequence && self.is(&[Tok::BlockEntry]) {
            self.state = State::IndentlessSequenceEntry;
            return Ok(Event::SequenceStart);
        }
        match self.peek().clone() {
            Tok::Scalar { value, plain } => {
                self.take();
                self.state = self.pop_state();
                Ok(Event::Scalar(value, plain))
            }
            Tok::FlowSequenceStart => {
                self.state = State::FlowSequenceFirstEntry;
                Ok(Event::SequenceStart)
            }
            Tok::FlowMappingStart => {
                self.state = State::FlowMappingFirstKey;
                Ok(Event::MappingStart)
            }
            Tok::BlockSequenceStart if block => {
                self.state = State::BlockSequenceFirstEntry;
                Ok(Event::SequenceStart)
            }
            Tok::BlockMappingStart if block => {
                self.state = State::BlockMappingFirstKey;
                Ok(Event::MappingStart)
            }
            _ => fail("expected the node content"),
        }
    }

    fn block_sequence_entry(&mut self) -> Res<Option<Event>> {
        if self.is(&[Tok::BlockEntry]) {
            self.take();
            if !self.is(&[Tok::BlockEntry, Tok::BlockEnd]) {
                self.states.push(State::BlockSequenceEntry);
                return self.node(true, false).map(Some);
            }
            self.state = State::BlockSequenceEntry;
            return Ok(Some(self.empty_scalar()));
        }
        if !self.is(&[Tok::BlockEnd]) {
            return fail("expected <block end>");
        }
        self.take();
        self.state = self.pop_state();
        Ok(Some(Event::SequenceEnd))
    }

    fn block_mapping_key(&mut self) -> Res<Option<Event>> {
        if self.is(&[Tok::Key]) {
            self.take();
            if !self.is(&[Tok::Key, Tok::Value, Tok::BlockEnd]) {
                self.states.push(State::BlockMappingValue);
                return self.node(true, true).map(Some);
            }
            self.state = State::BlockMappingValue;
            return Ok(Some(self.empty_scalar()));
        }
        if !self.is(&[Tok::BlockEnd]) {
            return fail("expected <block end>");
        }
        self.take();
        self.state = self.pop_state();
        Ok(Some(Event::MappingEnd))
    }

    fn flow_sequence_entry(&mut self, first: bool) -> Res<Option<Event>> {
        if !self.is(&[Tok::FlowSequenceEnd]) {
            if !first {
                if self.is(&[Tok::FlowEntry]) {
                    self.take();
                } else {
                    return fail("expected ',' or ']'");
                }
            }
            if self.is(&[Tok::Key]) {
                self.state = State::FlowSequenceEntryMappingKey;
                return Ok(Some(Event::MappingStart));
            } else if !self.is(&[Tok::FlowSequenceEnd]) {
                self.states.push(State::FlowSequenceEntry);
                return self.node(false, false).map(Some);
            }
        }
        self.take();
        self.state = self.pop_state();
        Ok(Some(Event::SequenceEnd))
    }

    fn flow_mapping_key(&mut self, first: bool) -> Res<Option<Event>> {
        if !self.is(&[Tok::FlowMappingEnd]) {
            if !first {
                if self.is(&[Tok::FlowEntry]) {
                    self.take();
                } else {
                    return fail("expected ',' or '}'");
                }
            }
            if self.is(&[Tok::Key]) {
                self.take();
                if !self.is(&[Tok::Value, Tok::FlowEntry, Tok::FlowMappingEnd]) {
                    self.states.push(State::FlowMappingValue);
                    return self.node(false, false).map(Some);
                }
                self.state = State::FlowMappingValue;
                return Ok(Some(self.empty_scalar()));
            } else if !self.is(&[Tok::FlowMappingEnd]) {
                self.states.push(State::FlowMappingEmptyValue);
                return self.node(false, false).map(Some);
            }
        }
        self.take();
        self.state = self.pop_state();
        Ok(Some(Event::MappingEnd))
    }
}

// ---------------------------------------------------------------- PyYAML resolvers

fn all_in(s: &str, ok: impl Fn(char) -> bool) -> bool {
    s.chars().all(ok)
}

fn digit(c: char) -> bool {
    c.is_ascii_digit()
}

fn digit_(c: char) -> bool {
    c.is_ascii_digit() || c == '_'
}

fn strip_sign(s: &str) -> &str {
    s.strip_prefix(['-', '+']).unwrap_or(s)
}

/// `[eE][-+][0-9]+` (the sign is required in PyYAML's float regex).
fn exponent_ok(s: &str) -> bool {
    let mut it = s.chars();
    matches!(it.next(), Some('e' | 'E')) && matches!(it.next(), Some('-' | '+')) && {
        let rest = it.as_str();
        !rest.is_empty() && all_in(rest, digit)
    }
}

/// `(:[0-5]?[0-9])+` exactly, given text starting at the first ':'.
fn sexagesimal_tail(s: &str) -> bool {
    if s.is_empty() {
        return false;
    }
    s.split(':').skip(1).all(|part| {
        let b = part.as_bytes();
        match b.len() {
            1 => b[0].is_ascii_digit(),
            2 => (b'0'..=b'5').contains(&b[0]) && b[1].is_ascii_digit(),
            _ => false,
        }
    }) && s.starts_with(':')
}

fn is_float(v: &str) -> bool {
    // [-+]?\.(inf|Inf|INF)  and  \.(nan|NaN|NAN)
    if matches!(strip_sign(v), ".inf" | ".Inf" | ".INF") || matches!(v, ".nan" | ".NaN" | ".NAN") {
        return true;
    }
    // \.[0-9][0-9_]*([eE][-+][0-9]+)?
    if let Some(rest) = v.strip_prefix('.') {
        let end = rest.find(|c: char| !digit_(c)).unwrap_or(rest.len());
        let (num, exp) = rest.split_at(end);
        return num.starts_with(|c: char| c.is_ascii_digit()) && (exp.is_empty() || exponent_ok(exp));
    }
    let body = strip_sign(v);
    if !body.starts_with(|c: char| c.is_ascii_digit()) {
        return false;
    }
    let head_end = body.find(|c: char| !digit_(c)).unwrap_or(body.len());
    let (_, rest) = body.split_at(head_end);
    // [0-9][0-9_]*\.[0-9_]*([eE][-+][0-9]+)?
    if let Some(after) = rest.strip_prefix('.') {
        let end = after.find(|c: char| !digit_(c)).unwrap_or(after.len());
        let exp = &after[end..];
        if exp.is_empty() || exponent_ok(exp) {
            return true;
        }
    }
    // [0-9][0-9_]*(:[0-5]?[0-9])+\.[0-9_]*
    if let Some(dot) = rest.find('.') {
        let (sexa, frac) = rest.split_at(dot);
        return sexagesimal_tail(sexa) && all_in(&frac[1..], digit_);
    }
    false
}

fn is_int(v: &str) -> bool {
    let body = strip_sign(v);
    if let Some(bits) = body.strip_prefix("0b") {
        return !bits.is_empty() && all_in(bits, |c| matches!(c, '0' | '1' | '_'));
    }
    if let Some(hex) = body.strip_prefix("0x") {
        return !hex.is_empty() && all_in(hex, |c| c.is_ascii_hexdigit() || c == '_');
    }
    if body == "0" {
        return true;
    }
    if let Some(oct) = body.strip_prefix('0') {
        return !oct.is_empty() && all_in(oct, |c| matches!(c, '0'..='7' | '_'));
    }
    if !body.starts_with(|c: char| matches!(c, '1'..='9')) {
        return false;
    }
    let end = body.find(|c: char| !digit_(c)).unwrap_or(body.len());
    let rest = &body[end..];
    rest.is_empty() || sexagesimal_tail(rest)
}

/// Hour, minute, second, and an optional UTC offset (hours, minutes).
type Clock = (u32, u32, u32, Option<(u32, u32)>);

struct Stamp {
    year: u32,
    month: u32,
    day: u32,
    time: Option<Clock>,
}

fn take_digits(s: &str, min: usize, max: usize) -> Option<(u32, &str)> {
    let n = s.bytes().take(max).take_while(u8::is_ascii_digit).count();
    if n < min {
        return None;
    }
    Some((s[..n].parse().ok()?, &s[n..]))
}

/// PyYAML's timestamp resolver regex, returning the groups its constructor reads.
fn timestamp(v: &str) -> Option<Stamp> {
    let (year, rest) = take_digits(v, 4, 4)?;
    let rest = rest.strip_prefix('-')?;
    // Date only: exactly YYYY-MM-DD.
    if rest.len() == 5 && rest.as_bytes()[2] == b'-'
        && rest[..2].bytes().all(|b| b.is_ascii_digit()) && rest[3..].bytes().all(|b| b.is_ascii_digit())
    {
        return Some(Stamp { year, month: rest[..2].parse().ok()?, day: rest[3..].parse().ok()?, time: None });
    }
    let (month, rest) = take_digits(rest, 1, 2)?;
    let rest = rest.strip_prefix('-')?;
    let (day, rest) = take_digits(rest, 1, 2)?;
    let rest = if let Some(r) = rest.strip_prefix(['T', 't']) {
        r
    } else {
        let trimmed = rest.trim_start_matches([' ', '\t']);
        if trimmed.len() == rest.len() {
            return None;
        }
        trimmed
    };
    let (hour, rest) = take_digits(rest, 1, 2)?;
    let (minute, rest) = take_digits(rest.strip_prefix(':')?, 2, 2)?;
    let (second, mut rest) = take_digits(rest.strip_prefix(':')?, 2, 2)?;
    if let Some(r) = rest.strip_prefix('.') {
        rest = r.trim_start_matches(|c: char| c.is_ascii_digit());
    }
    let zone = rest.trim_start_matches([' ', '\t']);
    let offset = if zone.is_empty() {
        if !rest.is_empty() {
            return None; // trailing blanks without a zone do not match
        }
        None
    } else if zone == "Z" {
        Some((0, 0))
    } else {
        let signed = zone.strip_prefix(['+', '-'])?;
        let (h, r) = take_digits(signed, 1, 2)?;
        let m = if r.is_empty() {
            0
        } else {
            let (m, r) = take_digits(r.strip_prefix(':')?, 2, 2)?;
            if !r.is_empty() {
                return None;
            }
            m
        };
        Some((h, m))
    };
    Some(Stamp { year, month, day, time: Some((hour, minute, second, offset)) })
}

fn valid_date(year: u32, month: u32, day: u32) -> bool {
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let days = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => return false,
    };
    (1..=9999).contains(&year) && (1..=days).contains(&day)
}

/// Resolve and construct one plain scalar the way PyYAML's SafeLoader does.
fn resolve_plain(v: &str) -> Result<Yaml, String> {
    let first = v.chars().next();
    if matches!(v, "true" | "True" | "TRUE") {
        return Ok(Yaml::Bool(true));
    }
    if matches!(v, "false" | "False" | "FALSE") {
        return Ok(Yaml::Bool(false));
    }
    if matches!(first, Some('-' | '+' | '.' | '0'..='9')) && is_float(v) {
        return Ok(Yaml::Other); // float construction cannot fail for these spellings
    }
    if matches!(first, Some('-' | '+' | '0'..='9')) && is_int(v) {
        let body: String = strip_sign(v).chars().filter(|&c| c != '_').collect();
        let empty_radix = body == "0b" || body == "0x";
        return if empty_radix { Err("invalid int literal".into()) } else { Ok(Yaml::Other) };
    }
    if v == "<<" || v == "=" {
        return Err("could not determine a constructor for the tag".into());
    }
    if matches!(v, "" | "~" | "null" | "Null" | "NULL") {
        return Ok(Yaml::Null);
    }
    if matches!(first, Some('0'..='9')) {
        if let Some(t) = timestamp(v) {
            if !valid_date(t.year, t.month, t.day) {
                return Err("invalid date".into());
            }
            if let Some((h, m, s, offset)) = t.time {
                if h > 23 || m > 59 || s > 59 {
                    return Err("invalid time".into());
                }
                if let Some((oh, om)) = offset {
                    if oh * 60 + om >= 24 * 60 {
                        return Err("invalid UTC offset".into());
                    }
                }
            }
            return Ok(Yaml::Other);
        }
    }
    Ok(Yaml::Str(v.to_string()))
}

enum Frame {
    Seq(Vec<Yaml>),
    Map(Vec<(String, Yaml)>, Option<String>),
}

/// The constructor's mapping rule from `discover_repo`: keys must be strings, never repeated.
fn attach(stack: &mut [Frame], root: &mut Option<Yaml>, node: Yaml) -> Res<()> {
    match stack.last_mut() {
        None => *root = Some(node),
        Some(Frame::Seq(items)) => items.push(node),
        Some(Frame::Map(entries, pending)) => match pending.take() {
            Some(key) => entries.push((key, node)),
            None => match node {
                Yaml::Str(key) => {
                    if entries.iter().any(|(k, _)| *k == key) {
                        return fail("non-string or duplicate YAML mapping key");
                    }
                    *pending = Some(key);
                }
                _ => return fail("non-string or duplicate YAML mapping key"),
            },
        },
    }
    Ok(())
}

/// Load one YAML document like `discover_repo.load_yaml`. `Ok(Yaml::Null)` for an empty stream.
pub fn load(data: &[u8]) -> Res<Yaml> {
    let tokens = scan(decode(data)?)?;
    let mut parser = Parser { tokens, at: 0, state: State::StreamStart, states: Vec::new() };
    let mut next = || -> Res<Event> { parser.next()?.ok_or_else(|| "unexpected end of events".to_string()) };
    if next()? != Event::StreamStart {
        return fail("expected stream start");
    }
    match next()? {
        Event::StreamEnd => return Ok(Yaml::Null),
        Event::DocumentStart => {}
        _ => return fail("expected document start"),
    }
    let mut stack: Vec<Frame> = Vec::new();
    let mut root: Option<Yaml> = None;
    while root.is_none() {
        match next()? {
            Event::Scalar(value, plain) => {
                let node = if plain { resolve_plain(&value)? } else { Yaml::Str(value) };
                attach(&mut stack, &mut root, node)?;
            }
            Event::SequenceStart => stack.push(Frame::Seq(Vec::new())),
            Event::MappingStart => stack.push(Frame::Map(Vec::new(), None)),
            Event::SequenceEnd | Event::MappingEnd => {
                let node = match stack.pop() {
                    Some(Frame::Seq(items)) => Yaml::Seq(items),
                    Some(Frame::Map(entries, _)) => Yaml::Map(entries),
                    None => return fail("unbalanced YAML events"),
                };
                attach(&mut stack, &mut root, node)?;
            }
            _ => return fail("unexpected YAML event"),
        }
    }
    if next()? != Event::DocumentEnd {
        return fail("expected document end");
    }
    if next()? != Event::StreamEnd {
        return fail("expected a single document in the stream");
    }
    Ok(root.unwrap_or(Yaml::Null))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(s: &str) -> Yaml {
        load(s.as_bytes()).unwrap_or_else(|e| panic!("{s:?}: {e}"))
    }

    #[test]
    fn scalars_resolve_like_pyyaml() {
        for (text, want) in [
            ("a: yes", Yaml::Str("yes".into())), ("a: on", Yaml::Str("on".into())),
            ("a: true", Yaml::Bool(true)), ("a: 08", Yaml::Str("08".into())),
            ("a: 1.0e5", Yaml::Str("1.0e5".into())), ("a: 1.0e+5", Yaml::Other),
            ("a: 1:30", Yaml::Other), ("a: 0o17", Yaml::Str("0o17".into())), ("a: 017", Yaml::Other),
            ("a: 1_000", Yaml::Other), ("a: .5", Yaml::Other), ("a: .inf", Yaml::Other),
            ("a: 2001-12-14t21:59:43.10-05:00", Yaml::Other), ("a: 2001-12-14", Yaml::Other),
            ("a: 2001-1-4", Yaml::Str("2001-1-4".into())), ("a: ~", Yaml::Null), ("a:", Yaml::Null),
            ("a: '1'", Yaml::Str("1".into())), ("a: \"true\"", Yaml::Str("true".into())),
        ] {
            assert_eq!(ok(text).get("a"), Some(&want), "{text}");
        }
    }

    #[test]
    fn refuses_what_python_refuses() {
        for text in [
            "a: &x 1\nb: *x", "a: !!str 5", "a: <<", "a: =", "<<: {b: 1}", "a: 2024-13-01",
            "a: 2024-01-01 10:00:00 +25:00", "a: 0b_", "a: 0x_", "--- 1\n--- 2", "a: 1\na: 2",
            "1: x", "? [a]\n: b", "a: \u{0001}",
        ] {
            assert!(load(text.as_bytes()).is_err(), "{text:?} should fail");
        }
    }

    #[test]
    fn empty_and_bom() {
        assert_eq!(ok(""), Yaml::Null);
        assert_eq!(ok("---"), Yaml::Null);
        assert_eq!(load("\u{FEFF}a: 1".as_bytes()).unwrap().get("a"), Some(&Yaml::Other));
        let utf16: Vec<u8> = [0xFFu8, 0xFE].into_iter()
            .chain("a: b".encode_utf16().flat_map(|u| u.to_le_bytes())).collect();
        assert_eq!(load(&utf16).unwrap().get("a"), Some(&Yaml::Str("b".into())));
    }

    #[test]
    fn pyyaml_line_breaks_and_tabs() {
        // NEL is a line break for PyYAML; a tab cannot start a token.
        assert!(load("a: |\n  x\u{85}y\n".as_bytes()).is_err());
        assert_eq!(ok("a: |\n  x\u{85}  y\n").get("a"), Some(&Yaml::Str("x\ny\n".into())));
        for text in ["a:\t\n  b: 1", "a: [x\ty]", "a: 1\t\n", "a: >\n  b\n[: x]"] {
            assert!(load(text.as_bytes()).is_err(), "{text:?} should fail");
        }
        assert_eq!(ok("a: 'x\n  y'").get("a"), Some(&Yaml::Str("x y".into())));
        assert_eq!(ok("a: \"x\\ty\"").get("a"), Some(&Yaml::Str("x\ty".into())));
        assert_eq!(ok("a: >-\n  one\n  two\n").get("a"), Some(&Yaml::Str("one two".into())));
        assert_eq!(ok("a: |+\n  one\n\n").get("a"), Some(&Yaml::Str("one\n\n".into())));
    }

    #[test]
    fn nesting_is_bounded() {
        let deep = format!("{}1{}", "[".repeat(65), "]".repeat(65));
        assert!(load(deep.as_bytes()).is_err());
        let fine = format!("{}1{}", "[".repeat(64), "]".repeat(64));
        assert!(load(fine.as_bytes()).is_ok());
    }
}

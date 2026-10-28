//! JSON helpers that reproduce Python's `json` module byte for byte, so the
//! Rust tools write the same files as the Python reference implementation.
//!
//! - `py_float` is Python's `repr(float)` (shortest round-trip digits, `1e-05`,
//!   `1e+16`, `100.0`).
//! - `py_round` is Python's `round(x, n)` (correctly rounded, ties to even).
//! - `dumps` is `json.dumps(value, sort_keys=True, ...)` with either compact
//!   separators or `indent=2`, and either `ensure_ascii` setting.
//! - `parse_strict` rejects duplicate object keys, like the Python readers'
//!   `object_pairs_hook=no_duplicates`.

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Number, Value};
use std::fmt;

/// Python `repr(x)` for a finite or non-finite float, as `json.dumps` writes it.
pub fn py_float(x: f64) -> String {
    if x.is_nan() {
        return "NaN".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    // Rust's `{:e}` prints the shortest digits that round-trip, like Python's repr.
    let sci = format!("{:e}", x);
    let (mantissa, exponent) = sci.split_once('e').expect("exponent form");
    let exponent: i32 = exponent.parse().expect("integer exponent");
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exponent) {
        let n = digits.len() as i32;
        let body = if exponent >= n - 1 {
            format!("{}{}.0", digits, "0".repeat((exponent - (n - 1)) as usize))
        } else if exponent >= 0 {
            let cut = (exponent + 1) as usize;
            format!("{}.{}", &digits[..cut], &digits[cut..])
        } else {
            format!("0.{}{}", "0".repeat((-exponent - 1) as usize), digits)
        };
        format!("{sign}{body}")
    } else {
        let head = &digits[..1];
        let tail = &digits[1..];
        let mantissa = if tail.is_empty() { head.to_string() } else { format!("{head}.{tail}") };
        let esign = if exponent < 0 { '-' } else { '+' };
        format!("{sign}{mantissa}e{esign}{:02}", exponent.abs())
    }
}

/// Python `round(x, places)` for finite floats.
pub fn py_round(x: f64, places: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    // Formatting with a precision rounds the exact binary value, ties to even,
    // which is what Python's correctly rounded `round` does.
    format!("{:.*}", places, x).parse().unwrap_or(x)
}

fn escape(out: &mut String, text: &str, ensure_ascii: bool) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ensure_ascii && !(' '..='~').contains(&c) => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn number(n: &Number) -> String {
    if let Some(i) = n.as_i64() {
        i.to_string()
    } else if let Some(u) = n.as_u64() {
        u.to_string()
    } else {
        py_float(n.as_f64().unwrap_or(f64::NAN))
    }
}

/// `json.dumps(value, sort_keys=True)`; `indent` is `None` for compact
/// `separators=(",", ":")`, or `Some(2)` for `indent=2`.
pub fn dumps(value: &Value, indent: Option<usize>, ensure_ascii: bool) -> String {
    let mut out = String::new();
    write(&mut out, value, indent, 0, ensure_ascii);
    out
}

fn write(out: &mut String, value: &Value, indent: Option<usize>, level: usize, ensure_ascii: bool) {
    let newline = |out: &mut String, level: usize| {
        if let Some(width) = indent {
            out.push('\n');
            out.push_str(&" ".repeat(width * level));
        }
    };
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&number(n)),
        Value::String(s) => escape(out, s, ensure_ascii),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                newline(out, level + 1);
                write(out, item, indent, level + 1, ensure_ascii);
            }
            newline(out, level);
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            for (i, key) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                newline(out, level + 1);
                escape(out, key, ensure_ascii);
                out.push_str(if indent.is_some() { ": " } else { ":" });
                write(out, &map[key], indent, level + 1, ensure_ascii);
            }
            newline(out, level);
            out.push('}');
        }
    }
}

/// A JSON value whose objects never contain a repeated key.
pub struct Strict(pub Value);

struct StrictVisitor;

impl<'de> Visitor<'de> for StrictVisitor {
    type Value = Strict;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("a JSON value")
    }
    fn visit_bool<E>(self, v: bool) -> Result<Strict, E> { Ok(Strict(Value::Bool(v))) }
    fn visit_i64<E>(self, v: i64) -> Result<Strict, E> { Ok(Strict(Value::from(v))) }
    fn visit_u64<E>(self, v: u64) -> Result<Strict, E> { Ok(Strict(Value::from(v))) }
    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Strict, E> {
        Number::from_f64(v).map(|n| Strict(Value::Number(n))).ok_or_else(|| E::custom("nonfinite number"))
    }
    fn visit_str<E>(self, v: &str) -> Result<Strict, E> { Ok(Strict(Value::String(v.to_owned()))) }
    fn visit_string<E>(self, v: String) -> Result<Strict, E> { Ok(Strict(Value::String(v))) }
    fn visit_unit<E>(self) -> Result<Strict, E> { Ok(Strict(Value::Null)) }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Strict, A::Error> {
        let mut items = Vec::new();
        while let Some(Strict(item)) = seq.next_element()? {
            items.push(item);
        }
        Ok(Strict(Value::Array(items)))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Strict, A::Error> {
        let mut map = Map::new();
        while let Some(key) = access.next_key::<String>()? {
            if map.contains_key(&key) {
                return Err(de::Error::custom("duplicate JSON key"));
            }
            let Strict(value) = access.next_value()?;
            map.insert(key, value);
        }
        Ok(Strict(Value::Object(map)))
    }
}

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_any(StrictVisitor)
    }
}

/// Parse UTF-8 JSON, refusing duplicate keys and non-finite numbers.
pub fn parse_strict(raw: &[u8]) -> Result<Value, String> {
    serde_json::from_slice::<Strict>(raw).map(|s| s.0).map_err(|e| e.to_string())
}

/// `type(value) is int` in Python: an integer that is not a bool.
pub fn as_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) if n.is_i64() || n.is_u64() => n.as_i64(),
        _ => None,
    }
}

/// `type(value) in (int, float)` in Python.
pub fn as_number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn float_repr_matches_python() {
        let cases = [
            (100.0, "100.0"), (0.1, "0.1"), (1e-5, "1e-05"), (1e16, "1e+16"), (1.5e16, "1.5e+16"),
            (1e15, "1000000000000000.0"), (0.0001, "0.0001"), (123.456, "123.456"),
            (-2.5, "-2.5"), (1e9, "1000000000.0"), (0.30000000000000004, "0.30000000000000004"),
            (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"),
        ];
        for (x, want) in cases {
            assert_eq!(py_float(x), want, "{x}");
        }
    }

    #[test]
    fn round_ties_to_even_on_exact_values() {
        assert_eq!(py_round(0.0625, 3), 0.062);
        assert_eq!(py_round(2.675, 2), 2.67); // binary value is below the tie
        assert_eq!(py_round(1.0005, 3), 1.0);
        assert_eq!(py_round(12.3456, 3), 12.346);
    }

    #[test]
    fn dumps_matches_python_layouts() {
        let v = json!({"b": [1, 2.5, {}], "a": "é\n", "c": []});
        assert_eq!(dumps(&v, None, true), "{\"a\":\"\\u00e9\\n\",\"b\":[1,2.5,{}],\"c\":[]}");
        assert_eq!(dumps(&v, None, false), "{\"a\":\"é\\n\",\"b\":[1,2.5,{}],\"c\":[]}");
        assert_eq!(dumps(&json!({"x": [1]}), Some(2), true), "{\n  \"x\": [\n    1\n  ]\n}");
    }

    #[test]
    fn strict_parse_refuses_duplicates() {
        assert!(parse_strict(br#"{"a":1,"a":2}"#).is_err());
        assert!(parse_strict(br#"{"a":{"b":1},"c":[{"b":2}]}"#).is_ok());
        assert_eq!(as_int(&json!(true)), None);
        assert_eq!(as_int(&json!(3)), Some(3));
        assert_eq!(as_int(&json!(3.0)), None);
    }
}

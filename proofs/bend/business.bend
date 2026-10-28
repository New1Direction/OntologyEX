# OntologyEX business laws, checked by Bend's prover.
#
# Models of business_view.rollup, evidence_grade.ladder, and the link
# decision rule in link_suggest. Each law is stated, then proven by the def
# of the same name; `bend business.bend` prints ALL PROOFS CHECK only when
# every law holds. Python and Rust are tested against the same rules
# (tests/conformance.py); this file is not a proof about that code.

import Base

# Linked systems and their rollup
# -------------------------------

type Status is Data:
  Ok{}
  Warn{}
  Err{}
  Stale{}
  Unk{}

type Sys is Data:
  Sys{status: Status, impacted: Bool}

type SL is Data:
  YNil{}
  YCon{h: Sys, t: SL}

type Health is Data:
  AtRisk{}
  Impacted{}
  Degraded{}
  StaleH{}
  Unknown{}
  Unlinked{}
  ReportedOk{}

type Summary is Data:
  Summary{err: Bool, imp: Bool, warn: Bool, stale: Bool, notok: Bool}

def one_st(st: Status, i: Bool) -> Summary:
  match st:
    case Ok{}:
      Summary{False{}, i, False{}, False{}, False{}}
    case Warn{}:
      Summary{False{}, i, True{}, False{}, True{}}
    case Err{}:
      Summary{True{}, i, False{}, False{}, True{}}
    case Stale{}:
      Summary{False{}, i, False{}, True{}, True{}}
    case Unk{}:
      Summary{False{}, i, False{}, False{}, True{}}

def one(s: Sys) -> Summary:
  match s:
    case Sys{st, i}:
      one_st(st, i)

def get_err(s: Summary) -> Bool:
  match s:
    case Summary{e, i, w, st, n}:
      e

def get_imp(s: Summary) -> Bool:
  match s:
    case Summary{e, i, w, st, n}:
      i

def get_warn(s: Summary) -> Bool:
  match s:
    case Summary{e, i, w, st, n}:
      w

def get_stale(s: Summary) -> Bool:
  match s:
    case Summary{e, i, w, st, n}:
      st

def get_notok(s: Summary) -> Bool:
  match s:
    case Summary{e, i, w, st, n}:
      n

# Field by field "any" over a list. Each field reduces on the head's value
# alone, so a known head decides it whatever the tail holds.
def merge(a: Summary, +b: Summary) -> Summary:
  match a:
    case Summary{e, i, w, st, n}:
      Summary{Bool.or(e, get_err(b)), Bool.or(i, get_imp(b)), Bool.or(w, get_warn(b)),
        Bool.or(st, get_stale(b)), Bool.or(n, get_notok(b))}

def rest(xs: SL) -> Summary:
  match xs:
    case YNil{}:
      Summary{False{}, False{}, False{}, False{}, False{}}
    case YCon{h, t}:
      merge(one(h), rest(t))

def nonempty(xs: SL) -> Bool:
  match xs:
    case YNil{}:
      False{}
    case YCon{h, t}:
      True{}

def classify(e: Bool, i: Bool, w: Bool, st: Bool, n: Bool) -> Health:
  match e:
    case True{}:
      AtRisk{}
    case False{}:
      match i:
        case True{}:
          Impacted{}
        case False{}:
          match w:
            case True{}:
              Degraded{}
            case False{}:
              match st:
                case True{}:
                  StaleH{}
                case False{}:
                  match n:
                    case True{}:
                      Unknown{}
                    case False{}:
                      ReportedOk{}

def pick(ne: Bool, +s: Summary) -> Health:
  match ne:
    case False{}:
      Unlinked{}
    case True{}:
      classify(get_err(s), get_imp(s), get_warn(s), get_stale(s), get_notok(s))

def rollup(+xs: SL) -> Health:
  pick(nonempty(xs), rest(xs))

def app(xs: SL, ys: SL) -> SL:
  match xs:
    case YNil{}:
      ys
    case YCon{h, t}:
      YCon{h, app(t, ys)}

def is_ok(h: Health) -> Bool:
  match h:
    case ReportedOk{}:
      True{}
    case _:
      False{}

# LAW: no linked system is UNLINKED, never a status.
law empty_is_unlinked:
  {rollup(YNil{}) == Unlinked{} : Health}

def empty_is_unlinked():
  {==}

# Lemmas: a list with an element in it is nonempty, and an erroring or
# not-ok element anywhere sets that summary field.
law nonempty_app:
  for xs: SL
  for s: Sys
  for ys: SL
  {True{} == nonempty(app(xs, YCon{s, ys})) : Bool}

def nonempty_app(xs, s, ys):
  match xs:
    case YNil{}:
      {==}
    case YCon{h, t}:
      {==}

law err_app:
  for xs: SL
  for i: Bool
  for ys: SL
  {True{} == get_err(rest(app(xs, YCon{Sys{Err{}, i}, ys}))) : Bool}

def err_app(xs, i, ys):
  match xs:
    case YNil{}:
      {==}
    case YCon{h, t}:
      match h:
        case Sys{st, im}:
          match st:
            case Ok{}:
              err_app(t, i, ys)
            case Warn{}:
              err_app(t, i, ys)
            case Err{}:
              {==}
            case Stale{}:
              err_app(t, i, ys)
            case Unk{}:
              err_app(t, i, ys)

law notok_app:
  for xs: SL
  for i: Bool
  for ys: SL
  {True{} == get_notok(rest(app(xs, YCon{Sys{Unk{}, i}, ys}))) : Bool}

def notok_app(xs, i, ys):
  match xs:
    case YNil{}:
      {==}
    case YCon{h, t}:
      match h:
        case Sys{st, im}:
          match st:
            case Ok{}:
              notok_app(t, i, ys)
            case Warn{}:
              {==}
            case Err{}:
              {==}
            case Stale{}:
              {==}
            case Unk{}:
              {==}

# LAW: one erroring linked system, anywhere in the list, makes the action AT RISK.
law error_anywhere_is_at_risk:
  for +xs: SL
  for +i: Bool
  for +ys: SL
  {rollup(app(xs, YCon{Sys{Err{}, i}, ys})) == AtRisk{} : Health}

def error_anywhere_is_at_risk(xs, i, ys):
  %nonempty_app(xs, Sys{Err{}, i}, ys) : {pick(_, rest(app(xs, YCon{Sys{Err{}, i}, ys}))) == AtRisk{} : Health}
  %err_app(xs, i, ys) : {classify(_, get_imp(rest(app(xs, YCon{Sys{Err{}, i}, ys}))), get_warn(rest(app(xs, YCon{Sys{Err{}, i}, ys}))), get_stale(rest(app(xs, YCon{Sys{Err{}, i}, ys}))), get_notok(rest(app(xs, YCon{Sys{Err{}, i}, ys})))) == AtRisk{} : Health}
  {==}

law not_ok_never_reported_ok:
  for e: Bool
  for i: Bool
  for w: Bool
  for st: Bool
  {is_ok(classify(e, i, w, st, True{})) == False{} : Bool}

def not_ok_never_reported_ok(e, i, w, st):
  match e:
    case True{}:
      {==}
    case False{}:
      match i:
        case True{}:
          {==}
        case False{}:
          match w:
            case True{}:
              {==}
            case False{}:
              match st:
                case True{}:
                  {==}
                case False{}:
                  {==}

# LAW: an unobserved (unknown) linked system, anywhere, never shows REPORTED OK.
law unknown_anywhere_is_not_ok:
  for +xs: SL
  for +i: Bool
  for +ys: SL
  {is_ok(rollup(app(xs, YCon{Sys{Unk{}, i}, ys}))) == False{} : Bool}

def unknown_anywhere_is_not_ok(xs, i, ys):
  %nonempty_app(xs, Sys{Unk{}, i}, ys) : {is_ok(pick(_, rest(app(xs, YCon{Sys{Unk{}, i}, ys})))) == False{} : Bool}
  %notok_app(xs, i, ys) : {is_ok(classify(get_err(rest(app(xs, YCon{Sys{Unk{}, i}, ys}))), get_imp(rest(app(xs, YCon{Sys{Unk{}, i}, ys}))), get_warn(rest(app(xs, YCon{Sys{Unk{}, i}, ys}))), get_stale(rest(app(xs, YCon{Sys{Unk{}, i}, ys}))), _)) == False{} : Bool}
  not_ok_never_reported_ok(get_err(rest(app(xs, YCon{Sys{Unk{}, i}, ys}))), get_imp(rest(app(xs, YCon{Sys{Unk{}, i}, ys}))), get_warn(rest(app(xs, YCon{Sys{Unk{}, i}, ys}))), get_stale(rest(app(xs, YCon{Sys{Unk{}, i}, ys}))))

# Evidence grade ladder
# ---------------------

type Grade is Data:
  G0{}
  G1{}
  G2{}
  G3{}
  G4{}

def ladder(rej: Bool, sup: Bool, obs: Bool, multi: Bool, failed: Bool, passed: Bool) -> Grade:
  match rej:
    case True{}:
      G0{}
    case False{}:
      match sup:
        case False{}:
          G0{}
        case True{}:
          match obs:
            case False{}:
              G1{}
            case True{}:
              match multi:
                case False{}:
                  G1{}
                case True{}:
                  match failed:
                    case True{}:
                      G2{}
                    case False{}:
                      match passed:
                        case False{}:
                          G2{}
                        case True{}:
                          G3{}

def is_g4(g: Grade) -> Bool:
  match g:
    case G4{}:
      True{}
    case _:
      False{}

def at_most_g1(g: Grade) -> Bool:
  match g:
    case G0{}:
      True{}
    case G1{}:
      True{}
    case _:
      False{}

# LAW: no recorded event can produce G4.
law ladder_never_g4:
  for rej: Bool
  for sup: Bool
  for obs: Bool
  for multi: Bool
  for failed: Bool
  for passed: Bool
  {is_g4(ladder(rej, sup, obs, multi, failed, passed)) == False{} : Bool}

def ladder_never_g4(rej, sup, obs, multi, failed, passed):
  match rej:
    case True{}:
      {==}
    case False{}:
      match sup:
        case False{}:
          {==}
        case True{}:
          match obs:
            case False{}:
              {==}
            case True{}:
              match multi:
                case False{}:
                  {==}
                case True{}:
                  match failed:
                    case True{}:
                      {==}
                    case False{}:
                      match passed:
                        case False{}:
                          {==}
                        case True{}:
                          {==}

# LAW: a rejecting review always gives G0.
law rejection_is_g0:
  for sup: Bool
  for obs: Bool
  for multi: Bool
  for failed: Bool
  for passed: Bool
  {ladder(True{}, sup, obs, multi, failed, passed) == G0{} : Grade}

def rejection_is_g0(sup, obs, multi, failed, passed):
  {==}

# LAW: an inferred rule stops at G1.
law inferred_at_most_g1:
  for rej: Bool
  for sup: Bool
  for multi: Bool
  for failed: Bool
  for passed: Bool
  {at_most_g1(ladder(rej, sup, False{}, multi, failed, passed)) == True{} : Bool}

def inferred_at_most_g1(rej, sup, multi, failed, passed):
  match rej:
    case True{}:
      {==}
    case False{}:
      match sup:
        case False{}:
          {==}
        case True{}:
          {==}

# Link decisions
# --------------

type Dec is Data:
  Confirm{}
  Reject{}

type DL is Data:
  DNil{}
  DCon{id: Nat, d: Dec, t: DL}

type GL is Data:
  GNil{}
  GCon{id: Nat, link: Nat, t: GL}

type LL is Data:
  LNil{}
  LCon{h: Nat, t: LL}

def confirms(d: Dec, b: Bool) -> Bool:
  match d:
    case Confirm{}:
      b
    case Reject{}:
      False{}

def confirmed(+id: Nat, ds: DL) -> Bool:
  match ds:
    case DNil{}:
      False{}
    case DCon{j, d, t}:
      Bool.or(confirms(d, Nat.is_eq(j, id)), confirmed(id, t))

def keep(b: Bool, l: Nat, rest: LL) -> LL:
  match b:
    case True{}:
      LCon{l, rest}
    case False{}:
      rest

def chosen(gs: GL, +ds: DL) -> LL:
  match gs:
    case GNil{}:
      LNil{}
    case GCon{i, l, t}:
      keep(confirmed(i, ds), l, chosen(t, ds))

def lapp(xs: LL, ys: LL) -> LL:
  match xs:
    case LNil{}:
      ys
    case LCon{h, t}:
      LCon{h, lapp(t, ys)}

def apply(links: LL, gs: GL, +ds: DL) -> LL:
  lapp(links, chosen(gs, ds))

law lapp_nil:
  for xs: LL
  {lapp(xs, LNil{}) == xs : LL}

def lapp_nil(xs):
  match xs:
    case LNil{}:
      {==}
    case LCon{h, t}:
      %lapp_nil(t) : {LCon{h, lapp(t, LNil{})} == LCon{h, _} : LL}
      {==}

law chosen_none:
  for gs: GL
  {LNil{} == chosen(gs, DNil{}) : LL}

def chosen_none(gs):
  match gs:
    case GNil{}:
      {==}
    case GCon{i, l, t}:
      chosen_none(t)

# LAW: suggestions alone never change the links.
law suggestions_alone_change_nothing:
  for +links: LL
  for gs: GL
  {apply(links, gs, DNil{}) == links : LL}

def suggestions_alone_change_nothing(links, gs):
  %chosen_none(gs) : {lapp(links, _) == links : LL}
  lapp_nil(links)

# LAW: a reject decision never confirms anything.
law reject_is_inert:
  for +id: Nat
  for j: Nat
  for ds: DL
  {confirmed(id, DCon{j, Reject{}, ds}) == confirmed(id, ds) : Bool}

def reject_is_inert(id, j, ds):
  {==}

# LAW: adding a reject decision never adds a link.
law reject_adds_nothing:
  for gs: GL
  for +j: Nat
  for +ds: DL
  {chosen(gs, DCon{j, Reject{}, ds}) == chosen(gs, ds) : LL}

def reject_adds_nothing(gs, j, ds):
  match gs:
    case GNil{}:
      {==}
    case GCon{i, l, t}:
      %reject_adds_nothing(t, j, ds) : {keep(confirmed(i, ds), l, chosen(t, DCon{j, Reject{}, ds})) == keep(confirmed(i, ds), l, _) : LL}
      {==}

#|ALL PROOFS CHECK

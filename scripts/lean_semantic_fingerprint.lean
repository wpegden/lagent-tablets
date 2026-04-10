import Lean

open Lean

private def nameFromString (s : String) : Name :=
  s.splitOn "." |>.foldl Name.str Name.anonymous

private partial def serializeLevel : Level → String
  | .zero => "z"
  | .succ l => s!"(s {serializeLevel l})"
  | .max a b => s!"(max {serializeLevel a} {serializeLevel b})"
  | .imax a b => s!"(imax {serializeLevel a} {serializeLevel b})"
  | .param n => s!"(param {n})"
  | .mvar _ => "(mvarlvl)"

private def serializeBinderInfo : BinderInfo → String
  | .default => "d"
  | .implicit => "i"
  | .strictImplicit => "si"
  | .instImplicit => "ii"

private partial def serializeExpr : Expr → String
  | .bvar idx => s!"(bvar {idx})"
  | .fvar fvarId => s!"(fvar {fvarId.name})"
  | .mvar _ => "(mvar)"
  | .sort lvl => s!"(sort {serializeLevel lvl})"
  | .const name lvls => s!"(const {name} [{String.intercalate "," (lvls.map serializeLevel)}])"
  | .app fn arg => s!"(app {serializeExpr fn} {serializeExpr arg})"
  | .lam _ ty body bi => s!"(lam {serializeBinderInfo bi} {serializeExpr ty} {serializeExpr body})"
  | .forallE _ ty body bi => s!"(forall {serializeBinderInfo bi} {serializeExpr ty} {serializeExpr body})"
  | .letE _ ty val body nondep => s!"(let {(if nondep then "1" else "0")} {serializeExpr ty} {serializeExpr val} {serializeExpr body})"
  | .lit lit => s!"(lit {repr lit})"
  | .mdata _ body => serializeExpr body
  | .proj typeName idx struct => s!"(proj {typeName} {idx} {serializeExpr struct})"

private partial def collectConsts : Expr → List Name
  | .const name _ => [name]
  | .app fn arg => collectConsts fn ++ collectConsts arg
  | .lam _ ty body _ => collectConsts ty ++ collectConsts body
  | .forallE _ ty body _ => collectConsts ty ++ collectConsts body
  | .letE _ ty val body _ => collectConsts ty ++ collectConsts val ++ collectConsts body
  | .mdata _ body => collectConsts body
  | .proj _ _ struct => collectConsts struct
  | _ => []

private def sortNames (names : List Name) : List Name :=
  (names.toArray.qsort (fun a b => toString a < toString b)).toList

private def uniqueSortedNames (names : List Name) : List Name :=
  let step (acc : Std.HashSet Name × List Name) (name : Name) :=
    if acc.1.contains name then
      acc
    else
      (acc.1.insert name, name :: acc.2)
  let (_, revOut) := (sortNames names).foldl step ({}, [])
  revOut.reverse

private structure FingerprintState where
  seen : Std.HashSet Name := {}
  lines : Array String := #[]

private def levelParamsString (params : List Name) : String :=
  String.intercalate "," (params.map toString)

private def childRefs (exprs : List Expr) : List Name :=
  uniqueSortedNames ((exprs.map collectConsts).foldl (fun acc xs => acc ++ xs) [])

private partial def visitConst (env : Environment) (name : Name) (st : FingerprintState) : Except String FingerprintState := do
  if st.seen.contains name then
    return st
  let st := { st with seen := st.seen.insert name }
  let some info := env.find? name
    | return { st with lines := st.lines.push s!"missing|{name}" }

  let (line, refs) :=
    match info with
    | .thmInfo v =>
        let line := s!"thm|lvls={levelParamsString v.levelParams}|type={serializeExpr v.type}"
        (line, childRefs [v.type])
    | .axiomInfo v =>
        let line := s!"axiom|lvls={levelParamsString v.levelParams}|type={serializeExpr v.type}"
        (line, childRefs [v.type])
    | .opaqueInfo v =>
        let line := s!"opaque|lvls={levelParamsString v.levelParams}|type={serializeExpr v.type}"
        (line, childRefs [v.type])
    | .defnInfo v =>
        let line := s!"def|lvls={levelParamsString v.levelParams}|type={serializeExpr v.type}|value={serializeExpr v.value}"
        (line, childRefs [v.type, v.value])
    | .quotInfo v =>
        let line := s!"quot|lvls={levelParamsString v.levelParams}|type={serializeExpr v.type}"
        (line, childRefs [v.type])
    | .inductInfo v =>
        let ctors := String.intercalate "," (v.ctors.map toString)
        let line := s!"induct|lvls={levelParamsString v.levelParams}|type={serializeExpr v.type}|ctors={ctors}|params={v.numParams}|indices={v.numIndices}"
        (line, childRefs (v.type :: v.ctors.map (fun ctor => Expr.const ctor [])))
    | .ctorInfo v =>
        let line := s!"ctor|lvls={levelParamsString v.levelParams}|type={serializeExpr v.type}|induct={v.induct}|cidx={v.cidx}|params={v.numParams}|fields={v.numFields}"
        (line, childRefs [v.type])
    | .recInfo v =>
        let line := s!"rec|lvls={levelParamsString v.levelParams}|type={serializeExpr v.type}"
        (line, childRefs [v.type])

  let st := { st with lines := st.lines.push s!"const|{name}|{line}" }
  refs.foldlM (init := st) fun acc child =>
    if child == name then
      return acc
    else
      visitConst env child acc

private def fingerprintPayloadFor (env : Environment) (declName : Name) : Except String String := do
  let st ← visitConst env declName {}
  let sortedLines := (st.lines.qsort (fun a b => a < b)).toList
  let payload := String.intercalate "||" (s!"root|{declName}" :: sortedLines)
  return payload

private def moduleForNode (nodeName : String) : Name :=
  nameFromString s!"Tablet.{nodeName}"

def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let nodeNames := args.toArray
  if nodeNames.isEmpty then
    IO.eprintln "ERR\t<global>\tno node names provided"
    return 1
  for nodeName in nodeNames do
    let declName := nameFromString nodeName
    try
      let env ← importModules #[{ module := moduleForNode nodeName }] {}
      match fingerprintPayloadFor env declName with
      | .ok payload =>
          IO.println s!"FP\t{nodeName}\t{payload}"
      | .error err =>
          IO.println s!"ERR\t{nodeName}\t{err}"
    catch e =>
      IO.println s!"ERR\t{nodeName}\t{e.toString}"
  return 0

import json
import os
import uuid
import threading
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Tuple, Optional, Set
from ortools.sat.python import cp_model

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@postgres:5432/postgres")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

class SolveDSLRequest(BaseModel):
    spec: Dict[str, Any]
    max_time_seconds: float = 15.0
    workers: int = 8

class CreateJobRequest(BaseModel):
    spec: Dict[str, Any]
    max_time_seconds: float = 60.0
    workers: int = 8



# --------------------------
# Utilities / Validation
# --------------------------

ALLOWED_KINDS = {
    # hard
    "exactly_one_assignment_per_day",
    "forbid_shift_sequences",
    "min_rest_minutes_between_shifts",
    "max_shifts_in_window",
    "max_work_minutes_in_window",
    "max_consecutive_work_days",
    "min_consecutive_days_off",
    # soft
    "penalize_work_on_days",
    "penalize_work_on_shifts",
    "penalize_unmet_day_off_requests",
    "fair_distribution",
}


def utcnow():
    return datetime.now(timezone.utc)

def db_insert_job(job_id: str, spec: Dict[str, Any], params: Dict[str, Any]):
    db = SessionLocal()
    try:
        db.execute(
            text("""
            INSERT INTO solver_jobs (job_id, status, spec_json, params_json)
            VALUES (:job_id, 'queued', CAST(:spec AS jsonb), CAST(:params AS jsonb))
            """),
            {"job_id": job_id, "spec": json.dumps(spec), "params": json.dumps(params)}
        )
        db.commit()
    finally:
        db.close()

def db_update_status(job_id: str, status: str, started_at=None, finished_at=None, error: str = None, result: Dict[str, Any] = None):
    db = SessionLocal()
    try:
        db.execute(
            text("""
            UPDATE solver_jobs
            SET status = :status,
                started_at = COALESCE(:started_at, started_at),
                finished_at = COALESCE(:finished_at, finished_at),
                error = COALESCE(:error, error),
                result_json = COALESCE(CAST(:result AS jsonb), result_json)
            WHERE job_id = :job_id
            """),
            {
                "job_id": job_id,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "error": error,
                "result": json.dumps(result) if result is not None else None
            }
        )
        db.commit()
    finally:
        db.close()

def db_get_job(job_id: str) -> Dict[str, Any] | None:
    db = SessionLocal()
    try:
        row = db.execute(
            text("""
            SELECT job_id, status, created_at, started_at, finished_at, error
            FROM solver_jobs
            WHERE job_id = :job_id
            """),
            {"job_id": job_id}
        ).mappings().first()
        return dict(row) if row else None
    finally:
        db.close()

def db_get_result(job_id: str) -> Dict[str, Any] | None:
    db = SessionLocal()
    try:
        row = db.execute(
            text("""
            SELECT status, result_json, error
            FROM solver_jobs
            WHERE job_id = :job_id
            """),
            {"job_id": job_id}
        ).mappings().first()
        return dict(row) if row else None
    finally:
        db.close()


def run_job(job_id: str, spec: Dict[str, Any], max_time_seconds: float, workers: int):
    try:
        db_update_status(job_id, "running", started_at=utcnow())

        result = compile_and_solve(spec, max_time_seconds, workers)
        if result.get("status") == "no_solution":
            db_update_status(job_id, "failed", finished_at=utcnow(), error="No feasible solution")
            return

        db_update_status(job_id, "done", finished_at=utcnow(), result=result)

    except Exception as e:
        db_update_status(job_id, "failed", finished_at=utcnow(), error=str(e))







def validate_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    # --- required top-level keys (minimo) ---
    if "sets" not in spec:
        return {"ok": False, "errors": ["Missing 'sets'"], "warnings": []}
    if "demand" not in spec:
        warnings.append("Missing 'demand' (no coverage constraints will be enforced).")
    if "constraints" not in spec:
        warnings.append("Missing 'constraints' (only demand constraints will be enforced).")

    sets = spec.get("sets", {})
    employees = sets.get("employees")
    days = sets.get("days")
    shifts = sets.get("shifts")
    sites = sets.get("sites", ["SITE_DEFAULT"])

    # --- sets basic checks ---
    if not isinstance(employees, list) or not employees:
        errors.append("sets.employees must be a non-empty list.")
        employees = []
    if not isinstance(days, list) or not days:
        errors.append("sets.days must be a non-empty list.")
        days = []
    if not isinstance(shifts, list) or not shifts:
        errors.append("sets.shifts must be a non-empty list.")
        shifts = []
    if not isinstance(sites, list) or not sites:
        errors.append("sets.sites must be a non-empty list (or omit to default).")
        sites = []

    # duplicates
    def dupes(lst: List[str]) -> List[str]:
        seen, d = set(), set()
        for x in lst:
            if x in seen:
                d.add(x)
            seen.add(x)
        return sorted(d)

    if employees:
        d = dupes(employees)
        if d:
            errors.append(f"Duplicate employee ids in sets.employees: {d}")
    if days:
        d = dupes(days)
        if d:
            errors.append(f"Duplicate day values in sets.days: {d}")
    if shifts:
        d = dupes(shifts)
        if d:
            errors.append(f"Duplicate shift ids in sets.shifts: {d}")
    if sites:
        d = dupes(sites)
        if d:
            errors.append(f"Duplicate site ids in sets.sites: {d}")

    # OFF shift required in our compiler
    if shifts and "OFF" not in shifts:
        errors.append("OFF shift must be present in sets.shifts for this compiler.")

    # --- shifts definitions checks ---
    shift_defs = spec.get("shifts", {})
    if not isinstance(shift_defs, dict):
        errors.append("Top-level 'shifts' must be an object/dict.")
        shift_defs = {}

    # For work shifts, ensure defs exist
    for s in shifts or []:
        if s not in shift_defs:
            if s == "OFF":
                warnings.append("shifts.OFF missing: compiler will assume default OFF=0 minutes.")
            else:
                warnings.append(f"Missing shifts['{s}'] definition (start/end/minutes).")

    # Validate format for defined shifts
    def _is_hhmm(v: str) -> bool:
        if not isinstance(v, str) or len(v) != 5 or v[2] != ":":
            return False
        hh, mm = v.split(":")
        return hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59

    for s, sd in shift_defs.items():
        if not isinstance(sd, dict):
            errors.append(f"shifts['{s}'] must be an object.")
            continue
        if "start" in sd and not _is_hhmm(sd["start"]):
            errors.append(f"shifts['{s}'].start must be HH:MM")
        if "end" in sd and not _is_hhmm(sd["end"]):
            errors.append(f"shifts['{s}'].end must be HH:MM")
        if "minutes" in sd and (not isinstance(sd["minutes"], int) or sd["minutes"] < 0):
            errors.append(f"shifts['{s}'].minutes must be a non-negative integer")

    # --- employees dictionary checks ---
    emp_defs = spec.get("employees", {})
    if emp_defs and not isinstance(emp_defs, dict):
        errors.append("Top-level 'employees' must be an object/dict if provided.")
        emp_defs = {}

    # warn if employee metadata missing
    for e in employees or []:
        if e not in emp_defs:
            warnings.append(f"employees['{e}'] missing (skills/roles/contract/site_home may be used by scope/requirements).")

    # helper sets for quick membership checks
    employees_set = set(employees or [])
    days_set = set(days or [])
    shifts_set = set(shifts or [])
    sites_set = set(sites or [])

    # --- demand checks ---
    for i, req in enumerate(spec.get("demand", []) or []):
        if not isinstance(req, dict):
            errors.append(f"demand[{i}] must be an object.")
            continue

        day = req.get("day")
        shift = req.get("shift")
        site = req.get("site", sites[0] if sites else None)

        if day not in days_set:
            errors.append(f"demand[{i}].day '{day}' not in sets.days")
        if shift not in shifts_set:
            errors.append(f"demand[{i}].shift '{shift}' not in sets.shifts")
        if site not in sites_set:
            errors.append(f"demand[{i}].site '{site}' not in sets.sites")

        # min/max/eq sanity
        if "eq" in req:
            if not isinstance(req["eq"], int) or req["eq"] < 0:
                errors.append(f"demand[{i}].eq must be an integer >= 0")
        else:
            if "min" in req and (not isinstance(req["min"], int) or req["min"] < 0):
                errors.append(f"demand[{i}].min must be an integer >= 0")
            if "max" in req and (not isinstance(req["max"], int) or req["max"] < 0):
                errors.append(f"demand[{i}].max must be an integer >= 0")
            if "min" in req and "max" in req and isinstance(req["min"], int) and isinstance(req["max"], int):
                if req["min"] > req["max"]:
                    errors.append(f"demand[{i}] has min > max")

        # requirements sanity (skills_min / roles_min)
        r = (req.get("requirements") or {})
        if not isinstance(r, dict):
            errors.append(f"demand[{i}].requirements must be an object if provided.")
            continue

        for j, sk in enumerate(r.get("skills_min", []) or []):
            if not isinstance(sk, dict):
                errors.append(f"demand[{i}].requirements.skills_min[{j}] must be an object.")
                continue
            if "skill" not in sk or "min" not in sk:
                errors.append(f"demand[{i}].requirements.skills_min[{j}] must have 'skill' and 'min'.")
                continue
            if not isinstance(sk["min"], int) or sk["min"] < 0:
                errors.append(f"demand[{i}].requirements.skills_min[{j}].min must be int >= 0")
            # warn if no employees have that skill
            skill = sk["skill"]
            have = [e for e in employees if skill in set((emp_defs.get(e, {}) or {}).get("skills", []))]
            if not have:
                warnings.append(f"demand[{i}] requires skill '{skill}' but no employee declares it.")

        for j, rl in enumerate(r.get("roles_min", []) or []):
            if not isinstance(rl, dict):
                errors.append(f"demand[{i}].requirements.roles_min[{j}] must be an object.")
                continue
            if "role" not in rl or "min" not in rl:
                errors.append(f"demand[{i}].requirements.roles_min[{j}] must have 'role' and 'min'.")
                continue
            if not isinstance(rl["min"], int) or rl["min"] < 0:
                errors.append(f"demand[{i}].requirements.roles_min[{j}].min must be int >= 0")
            role = rl["role"]
            have = [e for e in employees if role in set((emp_defs.get(e, {}) or {}).get("roles", []))]
            if not have:
                warnings.append(f"demand[{i}] requires role '{role}' but no employee declares it.")

    # --- constraints checks ---
    constraints = spec.get("constraints", []) or []
    if not isinstance(constraints, list):
        errors.append("constraints must be an array.")
        constraints = []

    ids = []
    for i, c in enumerate(constraints):
        if not isinstance(c, dict):
            errors.append(f"constraints[{i}] must be an object.")
            continue
        cid = c.get("id")
        ctype = c.get("type")
        kind = c.get("kind")
        scope = c.get("scope", {}) or {}
        data = c.get("data", {}) or {}
        pen = c.get("penalty", {}) or {}

        if not cid or not isinstance(cid, str):
            errors.append(f"constraints[{i}].id must be a string.")
        else:
            ids.append(cid)

        if ctype not in ("hard", "soft"):
            errors.append(f"constraints[{i}].type must be 'hard' or 'soft'.")
        if kind not in ALLOWED_KINDS:
            errors.append(f"constraints[{i}].kind '{kind}' not supported by this compiler.")
        # soft penalty checks
        if ctype == "soft":
            if "weight" not in pen:
                warnings.append(f"{cid}: soft constraint has no penalty.weight (will act like weight=0).")
            else:
                if not isinstance(pen.get("weight"), (int, float)) or pen.get("weight") < 0:
                    errors.append(f"{cid}: penalty.weight must be a non-negative number.")

        # scope sanity: if explicit employees list, ensure they exist
        if "employees" in scope and scope["employees"] != "ALL":
            if not isinstance(scope["employees"], list):
                errors.append(f"{cid}: scope.employees must be 'ALL' or a list.")
            else:
                missing = [e for e in scope["employees"] if e not in employees_set]
                if missing:
                    errors.append(f"{cid}: scope.employees contains unknown ids: {missing}")

        # Try to evaluate scope selection with our function (it uses groups/skills/roles/etc.)
        try:
            sel = select_employees_by_scope(spec, scope)
            if not sel:
                warnings.append(f"{cid}: scope selects 0 employees (constraint has no effect).")
        except Exception as ex:
            warnings.append(f"{cid}: scope could not be evaluated ({ex}).")

        # kind-specific validations (basic)
        if kind == "forbid_shift_sequences":
            pairs = data.get("forbidden_pairs", [])
            if not pairs:
                errors.append(f"{cid}: forbid_shift_sequences requires data.forbidden_pairs.")
            else:
                for p in pairs:
                    if p.get("prev_shift") not in shifts_set or p.get("next_shift") not in shifts_set:
                        errors.append(f"{cid}: forbidden pair uses shift not in sets.shifts: {p}")

        if kind in ("max_shifts_in_window", "max_work_minutes_in_window", "fair_distribution"):
            if "window_days" in data and (not isinstance(data["window_days"], int) or data["window_days"] <= 0):
                errors.append(f"{cid}: data.window_days must be integer > 0")

        if kind == "max_shifts_in_window":
            if "max" not in data or not isinstance(data["max"], int) or data["max"] < 0:
                errors.append(f"{cid}: max_shifts_in_window requires data.max as int >= 0")

        if kind == "max_work_minutes_in_window":
            if "max_minutes" not in data or not isinstance(data["max_minutes"], int) or data["max_minutes"] < 0:
                errors.append(f"{cid}: max_work_minutes_in_window requires data.max_minutes as int >= 0")

        if kind == "min_rest_minutes_between_shifts":
            if "min_rest_minutes" not in data or not isinstance(data["min_rest_minutes"], int) or data["min_rest_minutes"] < 0:
                errors.append(f"{cid}: min_rest_minutes_between_shifts requires data.min_rest_minutes as int >= 0")

        if kind == "max_consecutive_work_days":
            if "max" not in data or not isinstance(data["max"], int) or data["max"] < 0:
                errors.append(f"{cid}: max_consecutive_work_days requires data.max as int >= 0")

        if kind == "min_consecutive_days_off":
            if "min" not in data or not isinstance(data["min"], int) or data["min"] <= 0:
                errors.append(f"{cid}: min_consecutive_days_off requires data.min as int > 0")

        if kind == "penalize_work_on_days":
            dd = data.get("days", [])
            if not isinstance(dd, list) or not dd:
                errors.append(f"{cid}: penalize_work_on_days requires data.days list.")
            else:
                bad = [d for d in dd if d not in days_set]
                if bad:
                    errors.append(f"{cid}: penalize_work_on_days has unknown day(s): {bad}")

        if kind == "penalize_work_on_shifts":
            ss = data.get("shifts", [])
            if not isinstance(ss, list) or not ss:
                errors.append(f"{cid}: penalize_work_on_shifts requires data.shifts list.")
            else:
                bad = [s for s in ss if s not in shifts_set]
                if bad:
                    errors.append(f"{cid}: penalize_work_on_shifts has unknown shift(s): {bad}")

        if kind == "penalize_unmet_day_off_requests":
            dd = data.get("days", [])
            if not isinstance(dd, list) or not dd:
                errors.append(f"{cid}: penalize_unmet_day_off_requests requires data.days list.")
            else:
                bad = [d for d in dd if d not in days_set]
                if bad:
                    errors.append(f"{cid}: penalize_unmet_day_off_requests has unknown day(s): {bad}")

    # duplicate constraint ids
    if ids:
        d = dupes(ids)
        if d:
            errors.append(f"Duplicate constraint ids: {d}")

    # objective check (optional but useful)
    obj = spec.get("objective")
    if obj is None:
        warnings.append("Missing objective (solver will still run but only default objective).")
    else:
        if not isinstance(obj, dict):
            errors.append("objective must be an object.")
        else:
            mode = obj.get("mode")
            if mode and mode not in ("minimize", "maximize"):
                errors.append("objective.mode must be 'minimize' or 'maximize'")
            terms = obj.get("terms")
            if terms is not None and not isinstance(terms, list):
                errors.append("objective.terms must be an array")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings
    }


def day_index_map(days: List[str]) -> Dict[str, int]:
    return {d: i for i, d in enumerate(days)}

def get_employee(spec: Dict[str, Any], emp_id: str) -> Dict[str, Any]:
    return spec.get("employees", {}).get(emp_id, {})

def get_groups(spec: Dict[str, Any]) -> Dict[str, List[str]]:
    return spec.get("groups", {}) or {}

def normalize_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]

def select_employees_by_scope(spec: Dict[str, Any], scope: Dict[str, Any]) -> List[str]:
    """
    AND semantics across filters:
    - employees: ALL | [ids]
    - groups: [group_names]
    - skills_any / skills_all
    - roles_any / roles_all
    - sites_any (employee.site_home)
    - contracts_any (employee.contract.type)
    """
    all_emps = spec["sets"]["employees"]
    groups = get_groups(spec)

    # start set
    if not scope or scope.get("employees") == "ALL" or "employees" not in scope:
        selected: Set[str] = set(all_emps)
    else:
        selected = set(scope.get("employees", []))

    # groups
    for g in normalize_list(scope.get("groups")):
        selected &= set(groups.get(g, []))

    # skills
    skills_any = set(normalize_list(scope.get("skills_any")))
    skills_all = set(normalize_list(scope.get("skills_all")))
    if skills_any:
        selected &= {e for e in selected if skills_any.intersection(set(get_employee(spec, e).get("skills", [])))}
    if skills_all:
        selected &= {e for e in selected if skills_all.issubset(set(get_employee(spec, e).get("skills", [])))}

    # roles
    roles_any = set(normalize_list(scope.get("roles_any")))
    roles_all = set(normalize_list(scope.get("roles_all")))
    if roles_any:
        selected &= {e for e in selected if roles_any.intersection(set(get_employee(spec, e).get("roles", [])))}
    if roles_all:
        selected &= {e for e in selected if roles_all.issubset(set(get_employee(spec, e).get("roles", [])))}

    # sites_any by home site
    sites_any = set(normalize_list(scope.get("sites_any")))
    if sites_any:
        selected &= {e for e in selected if get_employee(spec, e).get("site_home") in sites_any}

    # contracts_any
    contracts_any = set(normalize_list(scope.get("contracts_any")))
    if contracts_any:
        def ctype(eid: str) -> Optional[str]:
            return (get_employee(spec, eid).get("contract") or {}).get("type")
        selected &= {e for e in selected if ctype(e) in contracts_any}

    return sorted(selected)


# --------------------------
# Time / shift helpers
# --------------------------

def parse_hhmm(hhmm: str) -> int:
    # minutes from 00:00
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)

def shift_interval_minutes(shift_def: Dict[str, Any]) -> Tuple[int, int, int]:
    """
    Returns (start_min, end_min, duration_min).
    Supports overnight shifts where end < start (wrap to next day).
    """
    start = parse_hhmm(shift_def["start"])
    end = parse_hhmm(shift_def["end"])
    dur = int(shift_def.get("minutes", 0))
    if dur == 0 and shift_def.get("is_work", True):
        # fallback: compute duration
        if end >= start:
            dur = end - start
        else:
            dur = (24*60 - start) + end
    return start, end, dur

def rest_minutes_between(shift_a: Dict[str, Any], shift_b: Dict[str, Any]) -> int:
    """
    Rest between shift_a (day d) and shift_b (day d+1).
    Uses shift end time of a and start time of b.
    If a ends after midnight (overnight), it reduces rest naturally.
    """
    a_start, a_end, a_dur = shift_interval_minutes(shift_a)
    b_start, b_end, b_dur = shift_interval_minutes(shift_b)

    # compute a_end absolute in [0, 2880) considering overnight end
    if parse_hhmm(shift_a["end"]) >= parse_hhmm(shift_a["start"]):
        a_end_abs = parse_hhmm(shift_a["end"])
    else:
        a_end_abs = 24*60 + parse_hhmm(shift_a["end"])

    # b_start on next day
    b_start_abs = 24*60 + parse_hhmm(shift_b["start"])

    return b_start_abs - a_end_abs


# --------------------------
# Compiler
# --------------------------

def compile_and_solve(spec: Dict[str, Any], max_time: float, workers: int) -> Dict[str, Any]:
    # basic sets
    employees: List[str] = spec["sets"]["employees"]
    days: List[str] = spec["sets"]["days"]
    shifts: List[str] = spec["sets"]["shifts"]
    sites: List[str] = spec["sets"].get("sites", ["SITE_DEFAULT"])

    shift_defs: Dict[str, Any] = spec.get("shifts", {})
    if "OFF" not in shifts:
        raise ValueError("This compiler expects OFF in sets.shifts.")
    if "OFF" not in shift_defs:
        # default OFF
        shift_defs["OFF"] = {"start": "00:00", "end": "00:00", "minutes": 0, "is_work": False}

    # validate kinds
    for c in spec.get("constraints", []):
        kind = c.get("kind")
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"Unsupported constraint kind: {kind}. Allowed: {sorted(ALLOWED_KINDS)}")

    day_to_idx = day_index_map(days)

    # determine work shifts (exclude OFF or any shift with is_work False)
    def is_work_shift(s: str) -> bool:
        return s != "OFF" and bool(shift_defs.get(s, {}).get("is_work", True))

    work_shifts = [s for s in shifts if is_work_shift(s)]

    model = cp_model.CpModel()

    # Variables:
    # - x[e,d,s,site] for work shifts
    # - off[e,d] for OFF
    x: Dict[Tuple[str, int, str, str], cp_model.IntVar] = {}
    off: Dict[Tuple[str, int], cp_model.IntVar] = {}

    for e in employees:
        for d in range(len(days)):
            off[(e, d)] = model.NewBoolVar(f"off_{e}_{d}")
            for s in work_shifts:
                for site in sites:
                    x[(e, d, s, site)] = model.NewBoolVar(f"x_{e}_{d}_{s}_{site}")

    # helper: sum over sites for a given (e,d,s)
    def works_shift(e: str, d: int, s: str) -> cp_model.LinearExpr:
        return sum(x[(e, d, s, site)] for site in sites)

    # helper: is working day (any work shift any site)
    def works_day(e: str, d: int) -> cp_model.LinearExpr:
        return sum(works_shift(e, d, s) for s in work_shifts)

    # --------------------------
    # Demand (coverage + requirements)
    # --------------------------
    # demand entry supports: min/max/eq, site, shift, requirements: skills_min, roles_min
    demand = spec.get("demand", [])
    for req in demand:
        d = day_to_idx[req["day"]]
        s = req["shift"]
        site = req.get("site", sites[0])

        if s not in work_shifts:
            raise ValueError(f"Demand references non-work shift '{s}'. Only work shifts: {work_shifts}")

        lhs = sum(x[(e, d, s, site)] for e in employees)

        if "eq" in req:
            model.Add(lhs == int(req["eq"]))
        else:
            if "min" in req:
                model.Add(lhs >= int(req["min"]))
            if "max" in req:
                model.Add(lhs <= int(req["max"]))

        # requirements
        r = req.get("requirements", {}) or {}

        # skills_min: [{"skill":"certified","min":1}]
        for sk in r.get("skills_min", []) or []:
            skill = sk["skill"]
            mn = int(sk["min"])
            lhs_skill = sum(
                x[(e, d, s, site)] for e in employees
                if skill in set(get_employee(spec, e).get("skills", []))
            )
            model.Add(lhs_skill >= mn)

        # roles_min: [{"role":"team_lead","min":1}]
        for rl in r.get("roles_min", []) or []:
            role = rl["role"]
            mn = int(rl["min"])
            lhs_role = sum(
                x[(e, d, s, site)] for e in employees
                if role in set(get_employee(spec, e).get("roles", []))
            )
            model.Add(lhs_role >= mn)

    # --------------------------
    # Constraints + soft penalties
    # --------------------------
    penalty_terms: List[cp_model.LinearExpr] = []

    for c in spec.get("constraints", []):
        cid = c["id"]
        ctype = c["type"]  # hard|soft
        kind = c["kind"]
        scope = c.get("scope", {}) or {}
        data = c.get("data", {}) or {}
        penalty = c.get("penalty", {}) or {}
        weight = int(penalty.get("weight", 0))

        emps = select_employees_by_scope(spec, scope)

        # ---- HARD ----
        if kind == "exactly_one_assignment_per_day":
            # Exactly one among: OFF OR any work assignment (any shift any site)
            # If data.shifts provided, restrict which work shifts are eligible.
            use_shifts = data.get("shifts")
            if use_shifts is None:
                use_shifts = work_shifts + ["OFF"]
            # build set of work shifts to count
            counted_work = [s for s in use_shifts if s != "OFF"]
            for e in emps:
                for d in range(len(days)):
                    lhs = off[(e, d)] + sum(works_shift(e, d, s) for s in counted_work)
                    model.Add(lhs == 1)

        elif kind == "forbid_shift_sequences":
            # forbidden_pairs: [{prev_shift, next_shift}]
            forbidden_pairs = data.get("forbidden_pairs", [])
            for e in emps:
                for d in range(len(days) - 1):
                    for p in forbidden_pairs:
                        prev_s = p["prev_shift"]
                        next_s = p["next_shift"]
                        if prev_s not in work_shifts or next_s not in work_shifts:
                            raise ValueError(f"{cid}: shifts must be work shifts (not OFF).")
                        model.Add(works_shift(e, d, prev_s) + works_shift(e, d + 1, next_s) <= 1)

        elif kind == "min_rest_minutes_between_shifts":
            # min_rest between any pair of (s_today, s_nextday) if rest < threshold then forbid
            min_rest = int(data["min_rest_minutes"])
            for e in emps:
                for d in range(len(days) - 1):
                    for s1 in work_shifts:
                        for s2 in work_shifts:
                            rest = rest_minutes_between(shift_defs[s1], shift_defs[s2])
                            if rest < min_rest:
                                model.Add(works_shift(e, d, s1) + works_shift(e, d + 1, s2) <= 1)

        elif kind == "max_shifts_in_window":
            window_days = int(data["window_days"])
            max_allowed = int(data["max"])
            counted = data.get("shifts", work_shifts)
            counted = [s for s in counted if s in work_shifts]
            mode = data.get("mode", "rolling")
            if mode != "rolling":
                raise ValueError(f"{cid}: only mode=rolling supported.")
            for e in emps:
                for start in range(len(days)):
                    window = range(start, min(start + window_days, len(days)))
                    expr = sum(works_shift(e, d, s) for d in window for s in counted)
                    model.Add(expr <= max_allowed)

        elif kind == "max_work_minutes_in_window":
            window_days = int(data["window_days"])
            max_minutes = int(data["max_minutes"])
            counted = data.get("shifts", work_shifts)
            counted = [s for s in counted if s in work_shifts]
            mode = data.get("mode", "rolling")
            if mode != "rolling":
                raise ValueError(f"{cid}: only mode=rolling supported.")

            shift_minutes = {s: int(shift_defs[s].get("minutes", 0)) for s in work_shifts}
            for e in emps:
                for start in range(len(days)):
                    window = range(start, min(start + window_days, len(days)))
                    expr = sum(shift_minutes[s] * works_shift(e, d, s) for d in window for s in counted)
                    model.Add(expr <= max_minutes)

        elif kind == "max_consecutive_work_days":
            max_consec = int(data["max"])
            for e in emps:
                # for each block of length max_consec+1, forbid all working
                L = max_consec + 1
                for start in range(0, len(days) - L + 1):
                    window = range(start, start + L)
                    model.Add(sum(works_day(e, d) for d in window) <= max_consec)

        elif kind == "min_consecutive_days_off":
            # If off starts at day d, enforce off for next (k-1) days (within horizon).
            # This is a common/usable encoding but not perfect for edge cases; still practical.
            k = int(data["min"])
            for e in emps:
                for d in range(len(days)):
                    # start_off = off[d] AND (d==0 OR not off[d-1])
                    start_off = model.NewBoolVar(f"{cid}_start_off_{e}_{d}")
                    if d == 0:
                        model.Add(start_off == off[(e, d)])
                    else:
                        # start_off <= off[d]
                        model.Add(start_off <= off[(e, d)])
                        # start_off <= 1 - off[d-1]
                        model.Add(start_off <= 1 - off[(e, d - 1)])
                        # start_off >= off[d] - off[d-1]
                        model.Add(start_off >= off[(e, d)] - off[(e, d - 1)])

                    # enforce k consecutive offs from start
                    for j in range(d, min(d + k, len(days))):
                        model.Add(off[(e, j)] == 1).OnlyEnforceIf(start_off)

        # ---- SOFT ----
        elif kind == "penalize_work_on_days":
            if ctype != "soft":
                raise ValueError(f"{cid}: penalize_work_on_days must be soft.")
            day_names = data["days"]
            target_days = [day_to_idx[n] for n in day_names]
            working_shifts = [s for s in data.get("working_shifts", work_shifts) if s in work_shifts]
            for e in emps:
                for d in target_days:
                    works = model.NewIntVar(0, 1, f"{cid}_works_{e}_{d}")
                    model.Add(works == sum(works_shift(e, d, s) for s in working_shifts))
                    penalty_terms.append(weight * works)

        elif kind == "penalize_work_on_shifts":
            if ctype != "soft":
                raise ValueError(f"{cid}: penalize_work_on_shifts must be soft.")
            target_shifts = [s for s in data.get("shifts", []) if s in work_shifts]
            for e in emps:
                for d in range(len(days)):
                    works = model.NewIntVar(0, 1, f"{cid}_w_{e}_{d}")
                    model.Add(works == sum(works_shift(e, d, s) for s in target_shifts))
                    penalty_terms.append(weight * works)

        elif kind == "penalize_unmet_day_off_requests":
            # requests: [{employee:"P1", days:[...]}] OR use scope employees + data.days
            if ctype != "soft":
                raise ValueError(f"{cid}: penalize_unmet_day_off_requests must be soft.")
            req_days = data.get("days")
            if req_days is None:
                raise ValueError(f"{cid}: needs data.days")
            target_days = [day_to_idx[n] for n in req_days]

            for e in emps:
                for d in target_days:
                    # penalty if not OFF => 1 - off
                    unmet = model.NewIntVar(0, 1, f"{cid}_unmet_{e}_{d}")
                    model.Add(unmet == 1 - off[(e, d)])
                    penalty_terms.append(weight * unmet)

        elif kind == "fair_distribution":
            # Support: measure=count, penalize=absolute_deviation, shifts=[...], window_days, target=auto_mean|number
            if ctype != "soft":
                raise ValueError(f"{cid}: fair_distribution must be soft.")
            measure = data.get("measure", "count")
            penalize = data.get("penalize", "absolute_deviation")
            counted_shifts = [s for s in data.get("shifts", []) if s in work_shifts]
            window_days = int(data.get("window_days", len(days)))
            target_mode = data.get("target", "auto_mean")
            if measure != "count" or penalize != "absolute_deviation":
                raise ValueError(f"{cid}: supported only measure=count and penalize=absolute_deviation.")
            if not counted_shifts:
                raise ValueError(f"{cid}: fair_distribution requires data.shifts.")

            # use rolling windows or whole horizon (here: whole horizon if window_days >= horizon)
            windows = []
            if window_days >= len(days):
                windows = [range(0, len(days))]
            else:
                windows = [range(start, min(start + window_days, len(days))) for start in range(len(days))]

            # Estimate total demand for those shifts across all sites/days:
            # If demand eq or min=max exists, use it; else fallback to 0 and compute target=0.
            total = 0
            for req in demand:
                if req["shift"] in counted_shifts:
                    if "eq" in req:
                        total += int(req["eq"])
                    elif "min" in req and "max" in req and int(req["min"]) == int(req["max"]):
                        total += int(req["min"])

            for window in windows:
                # compute target
                if target_mode == "auto_mean":
                    # rough mean over employees in scope
                    tgt = int(round(total / max(1, len(emps))))
                else:
                    tgt = int(target_mode)

                for e in emps:
                    cnt = model.NewIntVar(0, len(days), f"{cid}_cnt_{e}_{window.start if hasattr(window,'start') else 0}")
                    model.Add(cnt == sum(works_shift(e, d, s) for d in window for s in counted_shifts))
                    dev = model.NewIntVar(0, len(days), f"{cid}_dev_{e}_{window.start if hasattr(window,'start') else 0}")
                    model.Add(dev >= cnt - tgt)
                    model.Add(dev >= tgt - cnt)
                    penalty_terms.append(weight * dev)

        else:
            raise ValueError(f"Unsupported kind: {kind}")

    # --------------------------
    # Objective
    # --------------------------
    model.Minimize(sum(penalty_terms) if penalty_terms else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(max_time)
    solver.parameters.num_search_workers = int(workers)

    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "no_solution"}

    # --------------------------
    # Output schedule (day -> site -> shift -> employees) and metrics
    # --------------------------
    schedule = {day: {site: {s: [] for s in work_shifts} for site in sites} | {"OFF": []} for day in days}  # type: ignore

    for d, dayname in enumerate(days):
        # OFF
        for e in employees:
            if solver.Value(off[(e, d)]) == 1:
                schedule[dayname]["OFF"].append(e)  # type: ignore

        # work assignments
        for site in sites:
            for s in work_shifts:
                assigned = [e for e in employees if solver.Value(x[(e, d, s, site)]) == 1]
                schedule[dayname][site][s] = assigned  # type: ignore

    # metrics
    shift_minutes = {s: int(shift_defs[s].get("minutes", 0)) for s in work_shifts}
    minutes_worked = {}
    shift_counts = {}
    for e in employees:
        mins = 0
        counts = {s: 0 for s in work_shifts}
        for d in range(len(days)):
            for s in work_shifts:
                val = solver.Value(works_shift(e, d, s))
                if val:
                    mins += shift_minutes[s]
                    counts[s] += 1
        minutes_worked[e] = mins
        shift_counts[e] = counts

    return {
        "status": "ok",
        "objective": solver.ObjectiveValue(),
        "schedule": schedule,
        "metrics": {
            "minutes_worked": minutes_worked,
            "shift_counts": shift_counts
        }
    }


@app.post("/solve")
def solve(req: SolveDSLRequest):
    try:
        return compile_and_solve(req.spec, req.max_time_seconds, req.workers)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing field: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

@app.post("/validate")
def validate(req: SolveDSLRequest):
    try:
        return validate_spec(req.spec)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@app.post("/jobs")
def create_job(req: CreateJobRequest):
    # 1) validate spec (riusa validate_spec)
    v = validate_spec(req.spec)
    if not v["ok"]:
        raise HTTPException(status_code=400, detail={"message": "Spec invalid", "validation": v})

    # 2) insert job row
    job_id = str(uuid.uuid4())
    params = {"max_time_seconds": req.max_time_seconds, "workers": req.workers}
    db_insert_job(job_id, req.spec, params)

    # 3) start background thread
    t = threading.Thread(target=run_job, args=(job_id, req.spec, req.max_time_seconds, req.workers), daemon=True)
    t.start()

    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = db_get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # datetime -> isoformat
    for k in ["created_at", "started_at", "finished_at"]:
        if job.get(k) is not None:
            job[k] = job[k].isoformat()
    return job

@app.get("/jobs/{job_id}/result")
def get_job_result(job_id: str):
    r = db_get_result(job_id)
    if not r:
        raise HTTPException(status_code=404, detail="Job not found")

    if r["status"] != "done":
        return {"job_id": job_id, "status": r["status"], "error": r.get("error")}

    return {"job_id": job_id, "status": "done", "result": r["result_json"]}





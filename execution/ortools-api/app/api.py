from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Tuple, Optional, Set
from ortools.sat.python import cp_model

app = FastAPI()

class SolveDSLRequest(BaseModel):
    spec: Dict[str, Any]
    max_time_seconds: float = 15.0
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

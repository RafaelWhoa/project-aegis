#!/usr/bin/env python3
"""
Project Aegis - Synthetic DACH Hospital Database Seeder
=======================================================
Popula o PostgreSQL (schemas ehr_clinical e fin_billing) com 15.000 internações
realistas distribuídas ao longo de 2 anos (2024-01-01 a 2025-12-31) no padrão
do mercado hospitalar DACH (G-DRG / SwissDRG, EUR, Fallzusammenführung, SDOH /
Pflegegrad e sinais vitais com escore NEWS2).

Garantias anti-leakage e de realismo estatístico:
  - Cenário histórico baseline (care_program_id = NULL) sem treatment confounding.
  - Evolução temporal de comorbidades via encounters.charlson_score_at_admit.
  - Missingness estrutural realista (MNAR, ex: BNP concentrado em cardiologia).
  - Exames pendentes na alta (resulted_datetime > discharge_datetime em ~6%).
  - Taxa global de reinternação em 30 dias calibrada (~14%-18%) com correlações
    clínicas e financeiras realistas.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

# Permite execução tanto via `python src/seed_database.py` quanto `python -m src.seed_database`
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DACH_DEFAULTS, DB_CONFIG


# =============================================================================
# CONFIGURAÇÕES E CATÁLOGOS CLÍNICOS / FINANCEIROS (PADRÃO DACH - EUR)
# =============================================================================

SEED = 42
TARGET_ENCOUNTERS = 15_000
START_DATE = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
END_DATE = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
TOTAL_DAYS = (END_DATE - START_DATE).days
BASE_RATE = DACH_DEFAULTS.base_rate_eur

FIRST_NAMES_M = [
    "Lukas", "Maximilian", "Felix", "Jonas", "Leon", "Paul", "Tim",
    "Tobias", "Stefan", "Markus", "Andreas", "Christian", "Michael",
    "Thomas", "Daniel", "Florian", "Sebastian", "Johannes", "Martin", "Jan",
]
FIRST_NAMES_F = [
    "Anna", "Lena", "Sophie", "Marie", "Julia", "Laura", "Sarah",
    "Hannah", "Katharina", "Lisa", "Claudia", "Sabine", "Andrea",
    "Petra", "Monika", "Christine", "Melanie", "Nadine", "Franziska", "Eva",
]
LAST_NAMES = [
    "Mueller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner",
    "Becker", "Schulz", "Hoffmann", "Koch", "Bauer", "Richter", "Klein",
    "Wolf", "Schroeder", "Neumann", "Schwarz", "Zimmermann", "Braun",
]


@dataclass(frozen=True)
class GDRGSpec:
    drg_code: str
    description: str
    mdc_code: str
    drg_type: str
    relative_weight: float
    ugvd_days: float
    gmlos: float
    ogvd_days: float
    primary_icd10: str
    primary_desc: str
    department: str
    severity: float  # 0.0 (baixa) a 1.0 (alta)
    is_icu_prone: bool
    cluster: str

    @property
    def base_reimbursement(self) -> float:
        return round(self.relative_weight * BASE_RATE, 2)

    @property
    def standard_cost(self) -> float:
        # Custo padrão hospitalar típico ~91% do reembolso G-DRG base
        return round(self.base_reimbursement * 0.91, 2)


# Catálogo G-DRG realista (Alemanha / DACH, Basisfallwert €4.200)
DRG_CATALOG: list[GDRGSpec] = [
    GDRGSpec("F62A", "Heart Failure and Shock w Severe CC", "05", "MEDICAL", 1.5420, 2.0, 7.2, 16.0, "I50.14", "Left ventricular failure NYHA IV", "Cardiology", 0.90, True, "CHF"),
    GDRGSpec("F62B", "Heart Failure and Shock w CC", "05", "MEDICAL", 1.0150, 1.0, 5.4, 12.0, "I50.13", "Left ventricular failure NYHA III", "Cardiology", 0.60, False, "CHF"),
    GDRGSpec("F62C", "Heart Failure and Shock w/o Severe CC", "05", "MEDICAL", 0.7120, 1.0, 3.6, 8.0, "I50.9", "Heart failure, unspecified", "Cardiology", 0.30, False, "CHF"),
    GDRGSpec("T60A", "Septicemia w Complex Diagnosis or Severe CC", "18", "MEDICAL", 2.0840, 2.0, 8.4, 19.0, "A41.9", "Sepsis, unspecified organism", "Internal Medicine", 0.95, True, "SEPSIS"),
    GDRGSpec("T60B", "Septicemia w/o Severe CC", "18", "MEDICAL", 1.1480, 1.0, 5.5, 13.0, "A41.51", "Sepsis due to Escherichia coli", "Internal Medicine", 0.65, False, "SEPSIS"),
    GDRGSpec("E65A", "COPD w Severe CC / Acute Exacerbation", "04", "MEDICAL", 1.2460, 2.0, 6.4, 14.0, "J44.00", "COPD with acute lower respiratory infection", "Pulmonology", 0.80, True, "COPD"),
    GDRGSpec("E65B", "COPD w/o Severe CC", "04", "MEDICAL", 0.8240, 1.0, 4.5, 10.0, "J44.10", "COPD with acute exacerbation", "Pulmonology", 0.50, False, "COPD"),
    GDRGSpec("E77A", "Respiratory Infections & Pneumonia w Severe CC", "04", "MEDICAL", 1.4120, 2.0, 6.9, 15.0, "J18.9", "Pneumonia, unspecified organism", "Pulmonology", 0.78, True, "COPD"),
    GDRGSpec("E77B", "Respiratory Infections & Pneumonia w/o Severe CC", "04", "MEDICAL", 0.8850, 1.0, 4.8, 11.0, "J18.1", "Lobar pneumonia", "Internal Medicine", 0.45, False, "COPD"),
    GDRGSpec("L60A", "Renal Failure w Severe CC / Dialysis", "11", "MEDICAL", 1.6280, 2.0, 7.1, 16.0, "N17.9", "Acute kidney failure, unspecified", "Nephrology", 0.88, True, "RENAL"),
    GDRGSpec("L60B", "Renal Failure w/o Severe CC", "11", "MEDICAL", 0.9640, 1.0, 4.9, 11.0, "N18.4", "Chronic kidney disease, stage 4", "Nephrology", 0.56, False, "RENAL"),
    GDRGSpec("K60A", "Diabetes Mellitus w Severe Metabolic Complications", "10", "MEDICAL", 1.3850, 2.0, 6.2, 14.0, "E11.10", "Type 2 diabetes with ketoacidosis", "Endocrinology", 0.74, True, "RENAL"),
    GDRGSpec("K60B", "Diabetes Mellitus w/o Severe Complications", "10", "MEDICAL", 0.7980, 1.0, 4.1, 9.0, "E11.65", "Type 2 diabetes with hyperglycemia", "Endocrinology", 0.40, False, "RENAL"),
    GDRGSpec("F60A", "Acute Myocardial Infarction w Severe CC", "05", "MEDICAL", 1.7240, 2.0, 6.3, 14.0, "I21.4", "Non-ST elevation myocardial infarction", "Cardiology", 0.88, True, "CARDIAC"),
    GDRGSpec("F60B", "Acute Myocardial Infarction w/o Severe CC", "05", "MEDICAL", 1.0280, 1.0, 4.0, 9.0, "I21.4", "Non-ST elevation myocardial infarction", "Cardiology", 0.55, False, "CARDIAC"),
    GDRGSpec("B70A", "Cerebrovascular Accident (Stroke) w Severe CC", "01", "MEDICAL", 1.9650, 2.0, 7.8, 18.0, "I63.9", "Cerebral infarction, unspecified", "Neurology", 0.92, True, "NEURO"),
    GDRGSpec("B70B", "Cerebrovascular Accident (Stroke) w/o Severe CC", "01", "MEDICAL", 1.1240, 1.0, 5.1, 12.0, "I63.9", "Cerebral infarction, unspecified", "Neurology", 0.58, False, "NEURO"),
    GDRGSpec("G67A", "GI Hemorrhage & Ulcer Disease w Severe CC", "06", "MEDICAL", 1.5840, 2.0, 6.0, 14.0, "K92.2", "Gastrointestinal hemorrhage, unspecified", "Gastroenterology", 0.84, True, "GI"),
    GDRGSpec("G67B", "GI Hemorrhage & Ulcer Disease w/o Severe CC", "06", "MEDICAL", 0.9120, 1.0, 3.8, 9.0, "K92.2", "Gastrointestinal hemorrhage, unspecified", "Gastroenterology", 0.48, False, "GI"),
    GDRGSpec("I47A", "Hip / Knee Replacement w Severe CC", "08", "SURGICAL", 2.9400, 3.0, 7.6, 15.0, "M16.1", "Primary coxarthrosis, unilateral", "Orthopedics", 0.52, False, "ORTHO"),
    GDRGSpec("I47B", "Hip / Knee Replacement w/o Severe CC", "08", "SURGICAL", 1.8950, 2.0, 5.2, 10.0, "M17.1", "Primary gonarthrosis, unilateral", "Orthopedics", 0.22, False, "ORTHO"),
    GDRGSpec("G18B", "Major Small & Large Bowel Procedures w CC", "06", "SURGICAL", 2.4850, 3.0, 8.2, 17.0, "K57.32", "Diverticulitis of large intestine", "General Surgery", 0.62, False, "GI"),
    GDRGSpec("H08B", "Laparoscopic Cholecystectomy w/o Severe CC", "07", "SURGICAL", 1.1200, 1.0, 3.1, 7.0, "K80.10", "Calculus of gallbladder with cholecystitis", "General Surgery", 0.20, False, "ORTHO"),
]

DRG_BY_CLUSTER: dict[str, list[GDRGSpec]] = {}
for _d in DRG_CATALOG:
    DRG_BY_CLUSTER.setdefault(_d.cluster, []).append(_d)

CLUSTERS = list(DRG_BY_CLUSTER.keys())
CLUSTER_PROBS = [0.22, 0.16, 0.17, 0.14, 0.10, 0.07, 0.08, 0.06]

SECONDARY_ICDS = [
    "I10.00", "E11.90", "E78.5", "I48.9", "N18.3", "J44.9",
    "E66.00", "F32.9", "K21.9", "D64.9", "I25.1", "G47.31",
]

# Programas de transição de cuidados (EUR, capacidade operacional mensal e IC de eficácia para Monte Carlo)
# (code, name, desc, tier, cost_eur, duration_days, monthly_slots, staff_hrs, eff_mean, eff_low, eff_high)
CARE_PROGRAMS = [
    ("TCM-30",   "Transitional Care Management 30d",     "Structured GP handoff and 7/14-day outpatient follow-up.",        "MEDIUM",   340.00, 30, 120, 3.0,  0.18, 0.12, 0.24),
    ("TELE-HF",  "Remote Patient Telemonitoring",        "Daily home telemetry (weight, BP, SpO2) with nurse triage.",      "HIGH",     680.00, 45,  75, 5.5,  0.28, 0.20, 0.36),
    ("HOME-RN",  "Post-Discharge Nurse Home Visits",     "In-person transitional nursing visits within 72h and day 14.",    "HIGH",     920.00, 30,  50, 8.0,  0.32, 0.22, 0.42),
    ("MED-REC",  "Clinical Pharmacy Med Reconciliation", "Structured discharge medication reconciliation & day-7 call.",    "MEDIUM",   240.00, 30, 150, 2.0,  0.15, 0.09, 0.21),
    ("ICM-CRIT", "Intensive Multidisciplinary Case Mgmt","Combined telemonitoring, home nurse and physician coordination.", "CRITICAL", 1420.00, 60, 30, 12.0, 0.38, 0.28, 0.48),
]

# Exames laboratoriais: (loinc, name, category, unit, ref_low, ref_high, mean_norm, std_norm, abn_dir)
LAB_PANEL = [
    ("718-7",   "Hemoglobin", "HEMATOLOGY", "g/dL",    12.0,  16.0,  13.6,  1.1, -1),
    ("2951-2",  "Sodium",     "CHEMISTRY",  "mEq/L",  136.0, 145.0, 139.5,  2.2, -1),
    ("2160-0",  "Creatinine", "CHEMISTRY",  "mg/dL",    0.6,   1.2,   0.95, 0.18, +1),
    ("6690-2",  "WBC",        "HEMATOLOGY", "10^3/uL",  4.5,  11.0,   7.8,  1.8, +1),
    ("2345-7",  "Glucose",    "CHEMISTRY",  "mg/dL",   70.0, 140.0, 108.0, 18.0, +1),
    ("2823-3",  "Potassium",  "CHEMISTRY",  "mEq/L",    3.5,   5.1,   4.2,  0.35, +1),
    ("30934-4", "BNP",        "CARDIAC",    "pg/mL",    0.0, 100.0,  65.0, 25.0, +1),
]

MED_CATALOG = [
    ("11289",  "Phenprocoumon (Marcumar)", "Anticoagulant",     "3 mg",     "ORAL", "QD",   True),
    ("67108",  "Enoxaparin Sodium",        "Anticoagulant",     "40 mg",    "SC",   "QD",   True),
    ("274783", "Insulin Glargine",         "Antidiabetic",      "20 units", "SC",   "QD",   True),
    ("7804",   "Oxycodone HCl",            "Opioid Analgesic",  "5 mg",     "ORAL", "Q6H",  True),
    ("4603",   "Torasemide",               "Loop Diuretic",     "20 mg",    "ORAL", "QD",   True),
    ("3407",   "Digitoxin",                "Cardiac Glycoside", "0.07 mg",  "ORAL", "QD",   True),
    ("29046",  "Ramipril",                 "ACE Inhibitor",     "5 mg",     "ORAL", "QD",   False),
    ("6918",   "Bisoprolol Fumarate",      "Beta Blocker",      "5 mg",     "ORAL", "QD",   False),
    ("83367",  "Atorvastatin",             "Statin",            "40 mg",    "ORAL", "QD",   False),
    ("1191",   "Acetylsalicylic Acid",     "Antiplatelet",      "100 mg",   "ORAL", "QD",   False),
    ("7646",   "Pantoprazole",             "PPI",               "40 mg",    "ORAL", "QD",   False),
    ("18631",  "Amoxicillin/Clavulanate",  "Antibiotic",        "875 mg",   "ORAL", "BID",  False),
    ("2193",   "Ceftriaxone",              "Antibiotic",        "2 g",      "IV",   "Q24H", False),
    ("435",    "Salbutamol Sulfate",       "Bronchodilator",    "2.5 mg",   "INHALATION", "Q6H", False),
    ("6809",   "Metformin HCl",            "Antidiabetic",      "500 mg",   "ORAL", "BID",  False),
]


def sigmoid(x: float) -> float:
    return float(1.0 / (1.0 + np.exp(-np.clip(x, -20.0, 20.0))))


def classify_lab(value: float, low: float, high: float) -> str:
    if value < low * 0.82:
        return "CRITICAL_LOW"
    if value < low:
        return "LOW"
    if value > high * 1.45:
        return "CRITICAL_HIGH"
    if value > high:
        return "HIGH"
    return "NORMAL"


def compute_news2(hr: int, sbp: int, rr: int, spo2: float, temp: float) -> int:
    """Calcula escore clínico NEWS2 simplificado a partir dos sinais vitais."""
    score = 0
    # Respiratory rate
    if rr <= 8 or rr >= 25:
        score += 3
    elif 21 <= rr <= 24:
        score += 2
    elif 9 <= rr <= 11:
        score += 1
    # SpO2
    if spo2 <= 91.0:
        score += 3
    elif spo2 <= 93.0:
        score += 2
    elif spo2 <= 95.0:
        score += 1
    # Systolic BP
    if sbp <= 90 or sbp >= 220:
        score += 3
    elif 91 <= sbp <= 100:
        score += 2
    elif 101 <= sbp <= 110:
        score += 1
    # Heart rate
    if hr <= 40 or hr >= 131:
        score += 3
    elif 111 <= hr <= 130:
        score += 2
    elif (41 <= hr <= 50) or (91 <= hr <= 110):
        score += 1
    # Temperature
    if temp <= 35.0:
        score += 3
    elif temp >= 39.1:
        score += 2
    elif (35.1 <= temp <= 36.0) or (38.1 <= temp <= 39.0):
        score += 1
    return min(score, 20)


def sample_vitals(rng: np.random.Generator, instability: float, is_admit: bool) -> tuple[int, int, int, int, float, float, int]:
    """Gera sinais vitais coerentes com instabilidade clínica e calcula NEWS2."""
    stress = max(0.0, instability + (0.65 if is_admit else -0.35))
    hr = int(np.clip(rng.normal(76 + 7.5 * stress, 10.5), 42, 165))
    sbp = int(np.clip(rng.normal(126 - 5.0 * stress + rng.normal(0, 8), 16.0), 78, 210))
    dbp = int(np.clip(sbp * rng.uniform(0.54, 0.66), 45, min(125, sbp - 15)))
    rr = int(np.clip(rng.normal(16 + 1.8 * stress, 2.4), 10, 36))
    spo2 = round(float(np.clip(rng.normal(97.2 - 1.2 * stress, 1.5), 84.0, 100.0)), 1)
    temp = round(float(np.clip(rng.normal(36.7 + 0.35 * stress, 0.45), 35.0, 40.5)), 1)
    news2 = compute_news2(hr, sbp, rr, spo2, temp)
    return hr, sbp, dbp, rr, spo2, temp, news2


def sample_admit_datetime(rng: np.random.Generator) -> datetime:
    """Distribui admissões ao longo de 2 anos com leve sazonalidade de inverno europeu."""
    while True:
        day_offset = int(rng.integers(0, TOTAL_DAYS - 10))
        dt = START_DATE + timedelta(
            days=day_offset,
            hours=int(rng.integers(0, 24)),
            minutes=int(rng.integers(0, 60)),
        )
        # Pico leve no inverno europeu (dez-fev)
        seasonal_weight = 1.0 + 0.14 * np.cos(2 * np.pi * (dt.timetuple().tm_yday - 20) / 365.0)
        if rng.random() < seasonal_weight / 1.14:
            return dt


def pick_drg(rng: np.random.Generator, cluster: str, cci: int) -> GDRGSpec:
    """Pacientes com maior CCI têm maior probabilidade de cair em G-DRGs de severidade A."""
    candidates = DRG_BY_CLUSTER[cluster]
    if len(candidates) == 1:
        return candidates[0]
    ordered = sorted(candidates, key=lambda d: d.severity)
    severities = np.array([d.severity for d in ordered])
    cci_factor = (cci - 2.5) * 0.85
    logits = severities * cci_factor
    weights = np.exp(logits - logits.max())
    weights /= weights.sum()
    return ordered[int(rng.choice(len(ordered), p=weights))]


def generate_dataset(target_encounters: int = TARGET_ENCOUNTERS, seed: int = SEED) -> dict[str, list[tuple[Any, ...]]]:
    rng = np.random.default_rng(seed)

    # 1. Pool de pacientes (~8.250 pacientes para 15.000 internações)
    n_patients = int(target_encounters * 0.55)
    patients_rows: list[tuple[Any, ...]] = []
    patient_profiles: list[dict[str, Any]] = []

    ref_date = date(2025, 1, 1)
    for pid in range(1, n_patients + 1):
        sex = "F" if rng.random() < 0.52 else "M"
        fname = str(rng.choice(FIRST_NAMES_F if sex == "F" else FIRST_NAMES_M))
        lname = str(rng.choice(LAST_NAMES))

        age = int(np.clip(rng.normal(66, 15), 20, 96))
        birth_date = ref_date - timedelta(days=age * 365 + int(rng.integers(0, 365)))

        # CCI inicial (pode evoluir ao longo das internações sem vazar futuro!)
        cci_lambda = max(0.3, (age - 32) / 16.0 + rng.normal(0.0, 0.5))
        base_cci = int(np.clip(rng.poisson(cci_lambda), 0, 9))

        # Cobertura padrão DACH (GKV predominante ~82%, PKV ~13%, BG ~3%, SELF_PAY ~2%)
        payer_type = str(rng.choice(["GKV", "PKV", "BG", "SELF_PAY"], p=[0.82, 0.13, 0.03, 0.02]))
        postal_code = f"{int(rng.integers(10115, 99998))}"

        # Determinantes sociais (SDOH) correlacionados com idade e carga de doença
        lives_alone = bool(rng.random() < sigmoid(-1.2 + 0.028 * (age - 55)))
        pflege_lambda = max(0.05, (age - 62) / 18.0 + 0.18 * base_cci) if age >= 55 else 0.05
        nursing_care_level = int(np.clip(rng.poisson(pflege_lambda), 0, 5))
        distance_km = round(float(np.clip(rng.lognormal(mean=2.0, sigma=0.85), 0.5, 95.0)), 1)
        has_gp = bool(rng.random() < (0.91 if age >= 60 else 0.80))

        cluster = str(rng.choice(CLUSTERS, p=CLUSTER_PROBS))
        frailty = float(rng.normal(0.0, 0.45))

        patients_rows.append((
            pid, f"PAT-{pid:07d}", fname, lname, birth_date, sex, postal_code,
            payer_type, lives_alone, nursing_care_level, distance_km, has_gp,
            False, None,
        ))
        patient_profiles.append({
            "patient_id": pid,
            "age": age,
            "current_cci": base_cci,
            "payer_type": payer_type,
            "lives_alone": lives_alone,
            "nursing_care_level": nursing_care_level,
            "distance_km": distance_km,
            "has_gp": has_gp,
            "cluster": cluster,
            "frailty": frailty,
            "is_deceased": False,
        })

    # 2. Simular trajetórias longitudinais (baseline pré-programa -> zero treatment confounding)
    raw_encounters: list[dict[str, Any]] = []
    patient_order = rng.permutation(n_patients)
    cursor_idx = 0

    while len(raw_encounters) < target_encounters:
        if cursor_idx >= n_patients:
            patient_order = rng.permutation(n_patients)
            cursor_idx = 0

        prof = patient_profiles[int(patient_order[cursor_idx])]
        cursor_idx += 1
        if prof["is_deceased"]:
            continue

        admit_dt = sample_admit_datetime(rng)
        prior_admits_in_chain = 0

        while len(raw_encounters) < target_encounters and admit_dt <= END_DATE - timedelta(days=2):
            # Comorbidades podem progredir em internações subsequentes (snapshot temporal realista)
            if prior_admits_in_chain > 0 and rng.random() < 0.22:
                prof["current_cci"] = min(12, prof["current_cci"] + 1)
            cci = prof["current_cci"]
            age = prof["age"]

            cluster = prof["cluster"] if rng.random() < 0.78 else str(rng.choice(CLUSTERS, p=CLUSTER_PROBS))
            drg = pick_drg(rng, cluster, cci)

            is_icu = bool(drg.is_icu_prone and rng.random() < (0.16 + 0.045 * cci))
            encounter_type = "ICU" if is_icu else "INPATIENT"

            if drg.drg_type == "SURGICAL" and drg.severity < 0.50:
                admission_source = str(rng.choice(["ELECTIVE", "URGENT", "EMERGENCY"], p=[0.68, 0.18, 0.14]))
            else:
                emerg_prob = min(0.88, 0.50 + 0.04 * cci + 0.15 * drg.severity)
                admission_source = "EMERGENCY" if rng.random() < emerg_prob else str(
                    rng.choice(["URGENT", "TRANSFER", "REFERRAL", "ELECTIVE"], p=[0.38, 0.25, 0.22, 0.15])
                )

            # LOS compatível com G-DRG (uGVD / GMLOS / oGVD)
            los_mean = drg.gmlos * (1.0 + 0.06 * cci + (0.30 if is_icu else 0.0))
            los_days = float(np.clip(rng.gamma(shape=3.2, scale=los_mean / 3.2), max(1.0, drg.ugvd_days), drg.ogvd_days * 1.45))
            discharge_dt = admit_dt + timedelta(hours=int(round(los_days * 24)))
            if discharge_dt > END_DATE:
                discharge_dt = END_DATE

            discharge_instability = (
                0.24 * cci
                + 1.20 * drg.severity
                + (0.50 if is_icu else 0.0)
                + (0.30 if admission_source == "EMERGENCY" else 0.0)
                + 0.22 * prior_admits_in_chain
                + 0.15 * prof["nursing_care_level"]
                + prof["frailty"]
                + float(rng.normal(0.0, 0.75))
            )

            # Mortalidade intra-hospitalar (~3.5%)
            mort_prob = sigmoid(-5.5 + 0.62 * discharge_instability + 0.025 * (age - 65))
            if rng.random() < mort_prob:
                discharge_disposition = "EXPIRED"
                prof["is_deceased"] = True
            else:
                if discharge_instability > 2.6:
                    discharge_disposition = str(rng.choice(
                        ["SNF", "HOME_HEALTH", "REHAB", "HOME", "AMA"],
                        p=[0.30, 0.30, 0.16, 0.18, 0.06],
                    ))
                elif discharge_instability > 1.4:
                    discharge_disposition = str(rng.choice(
                        ["HOME_HEALTH", "HOME", "SNF", "REHAB", "AMA"],
                        p=[0.32, 0.44, 0.12, 0.08, 0.04],
                    ))
                else:
                    discharge_disposition = str(rng.choice(
                        ["HOME", "HOME_HEALTH", "REHAB"],
                        p=[0.78, 0.16, 0.06],
                    ))

            n_sec = int(np.clip(cci + rng.integers(0, 3), 0, 7))
            sec_codes = list(rng.choice(SECONDARY_ICDS, size=n_sec, replace=False)) if n_sec > 0 else []

            # Sinais vitais: admissão + pré-alta (e intermediário se LOS >= 5 dias)
            vitals_for_enc: list[dict[str, Any]] = []
            admit_v_dt = admit_dt + timedelta(minutes=int(rng.integers(15, 90)))
            hr, sbp, dbp, rr, spo2, temp, news2 = sample_vitals(rng, discharge_instability, is_admit=True)
            vitals_for_enc.append({
                "recorded_dt": admit_v_dt, "hr": hr, "sbp": sbp, "dbp": dbp,
                "rr": rr, "spo2": spo2, "temp": temp, "news2": news2,
            })
            if los_days >= 5.0:
                mid_v_dt = admit_dt + timedelta(hours=int(los_days * 12))
                hr, sbp, dbp, rr, spo2, temp, news2 = sample_vitals(rng, discharge_instability * 0.85, is_admit=False)
                vitals_for_enc.append({
                    "recorded_dt": mid_v_dt, "hr": hr, "sbp": sbp, "dbp": dbp,
                    "rr": rr, "spo2": spo2, "temp": temp, "news2": news2,
                })
            pre_dc_v_dt = max(admit_v_dt + timedelta(hours=1), discharge_dt - timedelta(hours=float(rng.uniform(1.5, 14.0))))
            hr, sbp, dbp, rr, spo2, temp, dc_news2 = sample_vitals(rng, discharge_instability, is_admit=False)
            vitals_for_enc.append({
                "recorded_dt": pre_dc_v_dt, "hr": hr, "sbp": sbp, "dbp": dbp,
                "rr": rr, "spo2": spo2, "temp": temp, "news2": dc_news2,
            })

            # Exames laboratoriais com missingness estrutural realista (MNAR)
            abn_prob_discharge = sigmoid(-1.85 + 0.68 * discharge_instability)
            labs_for_enc: list[dict[str, Any]] = []
            abnormal_discharge_count = 0

            # Painel básico sempre colhido (Hb, Na, Creat, WBC); Glucose ~85%, Potassium ~78%
            panel_indices = [0, 1, 2, 3]
            if rng.random() < 0.85:
                panel_indices.append(4)  # Glucose
            if rng.random() < 0.78:
                panel_indices.append(5)  # Potassium
            # BNP (MNAR): muito frequente em CHF/Cardiology, raro em Ortopedia/Cirurgia eletiva
            bnp_prob = 0.92 if drg.cluster == "CHF" else (0.72 if drg.cluster == "CARDIAC" else (0.30 if drg.cluster == "COPD" else 0.08))
            if rng.random() < bnp_prob:
                panel_indices.append(6)  # BNP

            timepoints = [("ADMIT", admit_dt + timedelta(hours=int(rng.integers(1, 5))))]
            if los_days >= 6.0:
                timepoints.append(("MID", admit_dt + timedelta(hours=int(los_days * 12))))
            hours_before_dc = float(rng.uniform(2.5, min(22.0, max(3.5, los_days * 12.0))))
            pre_dc_lab_dt = max(admit_dt + timedelta(minutes=45), discharge_dt - timedelta(hours=hours_before_dc))
            timepoints.append(("DISCHARGE", pre_dc_lab_dt))

            for tp_label, collected_dt in timepoints:
                is_dc_tp = tp_label == "DISCHARGE"
                for idx in panel_indices:
                    loinc, tname, tcat, unit, rlow, rhigh, mean_n, std_n, abn_dir = LAB_PANEL[idx]
                    p_abn = min(0.92, abn_prob_discharge * (1.25 if tp_label == "ADMIT" else 1.0))
                    if loinc == "30934-4" and drg.cluster == "CHF":
                        p_abn = min(0.95, p_abn + 0.25)
                    if loinc == "2160-0" and drg.cluster == "RENAL":
                        p_abn = min(0.95, p_abn + 0.25)

                    if rng.random() < p_abn:
                        severity_shift = float(rng.uniform(1.2, 3.0)) * (1.0 + 0.14 * max(0.0, discharge_instability))
                        if abn_dir < 0:
                            val = rlow - abs(rng.normal(0.85, 0.5)) * std_n * (0.7 + 0.2 * severity_shift)
                        else:
                            if loinc == "30934-4":
                                val = rhigh + abs(rng.normal(310.0, 210.0)) * (0.6 + 0.3 * severity_shift)
                            elif loinc == "2160-0":
                                val = rhigh + abs(rng.normal(0.85, 0.55)) * (0.7 + 0.25 * severity_shift)
                            else:
                                val = rhigh + abs(rng.normal(1.15, 0.6)) * std_n * (0.7 + 0.2 * severity_shift)
                    else:
                        val = float(rng.normal(mean_n, std_n * 0.78))

                    val = round(max(0.2 if loinc == "2160-0" else 1.0, val), 2)
                    flag = classify_lab(val, rlow, rhigh)

                    # ~6% dos exames pré-alta têm resultado liberado APÓS a alta (pendentes)
                    if is_dc_tp and rng.random() < 0.06:
                        resulted_dt = discharge_dt + timedelta(hours=float(rng.uniform(0.5, 6.0)))
                    else:
                        resulted_dt = min(
                            discharge_dt - timedelta(minutes=15) if is_dc_tp else collected_dt + timedelta(hours=3),
                            collected_dt + timedelta(minutes=int(rng.integers(35, 180))),
                        )
                        if resulted_dt < collected_dt:
                            resulted_dt = collected_dt + timedelta(minutes=20)

                    if is_dc_tp and flag != "NORMAL" and resulted_dt <= discharge_dt:
                        abnormal_discharge_count += 1

                    labs_for_enc.append({
                        "loinc": loinc, "name": tname, "category": tcat, "val": val,
                        "unit": unit, "rlow": rlow, "rhigh": rhigh, "flag": flag,
                        "collected_dt": collected_dt, "resulted_dt": resulted_dt,
                    })

            # Medicações
            n_meds = int(np.clip(2 + cci // 2 + rng.integers(1, 4), 2, 8))
            med_weights = np.array([(1.55 + 0.22 * cci) if m[6] else 1.0 for m in MED_CATALOG])
            med_weights /= med_weights.sum()
            chosen_med_idxs = rng.choice(len(MED_CATALOG), size=n_meds, replace=False, p=med_weights)
            meds_for_enc: list[dict[str, Any]] = []
            high_risk_med_count = 0
            for mi in chosen_med_idxs:
                rx, mname, tclass, dose, route, freq, is_hr = MED_CATALOG[int(mi)]
                order_type = "DISCHARGE" if (discharge_disposition != "EXPIRED" and rng.random() < 0.55) else "INPATIENT"
                if is_hr:
                    high_risk_med_count += 1
                med_start = admit_dt + timedelta(hours=int(rng.integers(0, 8)))
                med_end = discharge_dt if order_type == "INPATIENT" else discharge_dt + timedelta(days=30)
                meds_for_enc.append({
                    "rxnorm": rx, "name": mname, "tclass": tclass, "dose": dose,
                    "route": route, "freq": freq, "order_type": order_type,
                    "is_hr": is_hr, "start_dt": med_start, "end_dt": med_end,
                })

            # Custos reais hospitalares em EUR (G-DRG + LOS vs GMLOS + UTI)
            los_ratio = los_days / max(1.0, drg.gmlos)
            cost_mult = float(np.clip(
                0.76 + 0.28 * los_ratio + 0.03 * cci + (0.24 if is_icu else 0.0) + rng.normal(0.0, 0.11),
                0.65, 2.75,
            ))
            total_cost = round(drg.standard_cost * cost_mult, 2)
            total_charges = round(total_cost * float(rng.uniform(1.22, 1.48)), 2)
            expected_reimb = round(drg.base_reimbursement * float(rng.uniform(0.95, 1.08)), 2)

            # Modelo logístico calibrado (baseline SEM intervenção -> zero treatment confounding!)
            # Inclui ruído clínico para AUC realista (~0.76-0.82)
            readm_logit = (
                -5.60
                + 0.20 * cci
                + 0.28 * abnormal_discharge_count
                + 0.16 * dc_news2
                + 0.38 * min(prior_admits_in_chain, 3)
                + 0.35 * (1.0 if admission_source == "EMERGENCY" else 0.0)
                + 0.42 * np.log(drg.standard_cost / 5_000.0)
                + 0.18 * high_risk_med_count
                + 0.18 * (1.0 if prof["lives_alone"] else 0.0)
                + 0.14 * prof["nursing_care_level"]
                - 0.25 * (1.0 if prof["has_gp"] else 0.0)
                + (0.50 if discharge_disposition == "AMA" else 0.0)
                + (0.22 if discharge_disposition == "SNF" else 0.0)
                + 0.45 * prof["frailty"]
                + float(rng.normal(0.0, 0.65))  # ruído não observável realista
            )
            p_readmit_30d = 0.0 if discharge_disposition in ("EXPIRED", "HOSPICE") else sigmoid(readm_logit)
            will_readmit_30d = bool(rng.random() < p_readmit_30d)

            raw_encounters.append({
                "patient_id": prof["patient_id"],
                "payer_type": prof["payer_type"],
                "drg": drg,
                "cci_at_admit": cci,
                "encounter_type": encounter_type,
                "admit_dt": admit_dt,
                "discharge_dt": discharge_dt,
                "admission_source": admission_source,
                "discharge_disposition": discharge_disposition,
                "sec_codes": sec_codes,
                "physician_id": f"ARZT-{int(rng.integers(100, 350))}",
                "vitals": vitals_for_enc,
                "labs": labs_for_enc,
                "meds": meds_for_enc,
                "total_charges": total_charges,
                "total_cost": total_cost,
                "expected_reimb": expected_reimb,
            })

            if discharge_disposition in ("EXPIRED", "HOSPICE"):
                break

            if will_readmit_30d:
                gap_days = int(rng.integers(2, 29))
                admit_dt = discharge_dt + timedelta(days=gap_days, hours=int(rng.integers(1, 20)))
                prior_admits_in_chain += 1
            elif rng.random() < sigmoid(-1.40 + 0.20 * cci + 0.35 * prof["frailty"]):
                gap_days = int(rng.integers(35, 220))
                admit_dt = discharge_dt + timedelta(days=gap_days, hours=int(rng.integers(1, 20)))
                prior_admits_in_chain += 1
            else:
                break

    # 3. Ordenar cronologicamente e aplicar regra DACH de Fallzusammenführung (Case Consolidation)
    raw_encounters.sort(key=lambda e: (e["admit_dt"], e["patient_id"]))

    last_enc_by_patient: dict[int, tuple[int, datetime, str]] = {}
    encounters_rows: list[tuple[Any, ...]] = []
    vital_rows: list[tuple[Any, ...]] = []
    lab_rows: list[tuple[Any, ...]] = []
    med_rows: list[tuple[Any, ...]] = []
    billing_rows: list[tuple[Any, ...]] = []
    deceased_updates: dict[int, date] = {}

    payer_names_map = {
        "GKV": ["AOK", "Techniker Krankenkasse (TK)", "BARMER", "DAK-Gesundheit", "ÖGK"],
        "PKV": ["Allianz Private Krankenversicherung", "Debeka", "DKV", "AXA Krankenversicherung"],
        "BG": ["Berufsgenossenschaft (DGUV)", "AUVA"],
        "SELF_PAY": ["Selbstzahler (Self-Pay)"],
    }

    for eid, enc in enumerate(raw_encounters, start=1):
        pid = enc["patient_id"]
        admit_dt = enc["admit_dt"]
        discharge_dt = enc["discharge_dt"]
        drg: GDRGSpec = enc["drg"]
        prev_eid: int | None = None
        is_consolidated = False

        if pid in last_enc_by_patient:
            prev_id, prev_dc, prev_mdc = last_enc_by_patient[pid]
            if admit_dt <= prev_dc:
                shift = (prev_dc - admit_dt) + timedelta(hours=int(rng.integers(6, 36)))
                admit_dt = admit_dt + shift
                discharge_dt = discharge_dt + shift
                for vt in enc["vitals"]:
                    vt["recorded_dt"] = vt["recorded_dt"] + shift
                for lb in enc["labs"]:
                    lb["collected_dt"] = lb["collected_dt"] + shift
                    lb["resulted_dt"] = lb["resulted_dt"] + shift
                for md in enc["meds"]:
                    md["start_dt"] = md["start_dt"] + shift
                    md["end_dt"] = md["end_dt"] + shift

            prev_eid = prev_id
            # Regra Fallzusammenführung (§ 2 FPV): readmissão em <= 30 dias no mesmo MDC ou complicação
            if (admit_dt - prev_dc) <= timedelta(days=DACH_DEFAULTS.consolidation_window_days):
                if drg.mdc_code == prev_mdc or rng.random() < 0.75:
                    is_consolidated = True

        last_enc_by_patient[pid] = (eid, discharge_dt, drg.mdc_code)

        if enc["discharge_disposition"] == "EXPIRED":
            deceased_updates[pid] = discharge_dt.date()

        encounters_rows.append((
            eid, pid, prev_eid, f"FALL-{admit_dt.year}-{eid:06d}",
            enc["encounter_type"], admit_dt, discharge_dt,
            enc["admission_source"], enc["discharge_disposition"],
            drg.department, drg.primary_icd10, drg.primary_desc,
            enc["sec_codes"], enc["cci_at_admit"], enc["physician_id"],
        ))

        for vt in enc["vitals"]:
            vital_rows.append((
                eid, pid, vt["recorded_dt"], vt["hr"], vt["sbp"],
                vt["dbp"], vt["rr"], vt["spo2"], vt["temp"], vt["news2"],
            ))

        for lb in enc["labs"]:
            lab_rows.append((
                eid, pid, lb["loinc"], lb["name"], lb["category"], lb["val"],
                None, lb["unit"], lb["rlow"], lb["rhigh"], lb["flag"],
                lb["collected_dt"], lb["resulted_dt"],
            ))

        for md in enc["meds"]:
            med_rows.append((
                eid, pid, md["rxnorm"], md["name"], md["tclass"], md["dose"],
                md["route"], md["freq"], md["order_type"], md["is_hr"],
                md["start_dt"], md["end_dt"], enc["physician_id"],
            ))

        # Faturamento DACH (EUR): quando há Fallzusammenführung, o hospital perde o 2º reembolso DRG
        billing_date = discharge_dt.date() + timedelta(days=int(rng.integers(3, 14)))
        if is_consolidated:
            claim_status = "CONSOLIDATED"
            consolidation_loss = enc["expected_reimb"]
            paid_amount = 0.00
            paid_date = None
        else:
            claim_status = str(rng.choice(["PAID", "APPROVED", "PARTIAL", "SUBMITTED", "DENIED"], p=[0.75, 0.10, 0.06, 0.06, 0.03]))
            consolidation_loss = 0.00
            if claim_status == "PAID":
                paid_amount = enc["expected_reimb"]
                paid_date = billing_date + timedelta(days=int(rng.integers(14, 42)))
            elif claim_status == "PARTIAL":
                paid_amount = round(enc["expected_reimb"] * float(rng.uniform(0.65, 0.88)), 2)
                paid_date = billing_date + timedelta(days=int(rng.integers(20, 55)))
            else:
                paid_amount = 0.00
                paid_date = None

        payer_type = enc["payer_type"]
        payer_name = str(rng.choice(payer_names_map[payer_type]))

        billing_rows.append((
            eid, pid, drg.drg_code, None,  # care_program_id = NULL (baseline limpo)
            payer_name, payer_type, claim_status, DACH_DEFAULTS.currency,
            enc["total_charges"], enc["total_cost"], enc["expected_reimb"],
            paid_amount, is_consolidated, consolidation_loss,
            billing_date, paid_date,
        ))

    updated_patients: list[tuple[Any, ...]] = []
    for row in patients_rows:
        pid = row[0]
        if pid in deceased_updates:
            dec_date = max(row[4], deceased_updates[pid])
            updated_patients.append((*row[:12], True, dec_date))
        else:
            updated_patients.append(row)

    drg_rows = [
        (
            d.drg_code, d.description, d.mdc_code, d.drg_type,
            d.relative_weight, BASE_RATE, d.ugvd_days, d.gmlos, d.ogvd_days,
            DACH_DEFAULTS.consolidation_window_days, d.standard_cost,
            d.base_reimbursement, DACH_DEFAULTS.currency, date(2024, 1, 1), None,
        )
        for d in DRG_CATALOG
    ]

    care_program_rows = [
        (
            idx, code, name, desc, tier, cost, DACH_DEFAULTS.currency,
            dur, slots, hrs, eff_mean, eff_low, eff_high, True,
            date(2024, 1, 1), None,
        )
        for idx, (code, name, desc, tier, cost, dur, slots, hrs, eff_mean, eff_low, eff_high)
        in enumerate(CARE_PROGRAMS, start=1)
    ]

    return {
        "drg_costs": drg_rows,
        "care_programs": care_program_rows,
        "patients": updated_patients,
        "encounters": encounters_rows,
        "vital_signs": vital_rows,
        "lab_results": lab_rows,
        "medications": med_rows,
        "encounter_billing": billing_rows,
    }


def load_into_postgres(data: dict[str, list[tuple[Any, ...]]], reset: bool = True) -> None:
    engine = create_engine(DB_CONFIG.sqlalchemy_url, future=True)

    with engine.begin() as conn:
        if reset:
            conn.execute(text("""
                TRUNCATE TABLE
                    analytics.model_predictions,
                    fin_billing.encounter_billing,
                    fin_billing.care_programs,
                    fin_billing.drg_costs,
                    ehr_clinical.medications,
                    ehr_clinical.lab_results,
                    ehr_clinical.vital_signs,
                    ehr_clinical.encounters,
                    ehr_clinical.patients
                RESTART IDENTITY CASCADE;
            """))

        raw_conn = conn.connection
        with raw_conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO fin_billing.drg_costs (
                    drg_code, drg_description, mdc_code, drg_type,
                    relative_weight, base_rate, ugvd_days, geometric_mean_los,
                    ogvd_days, consolidation_window_days, standard_cost,
                    base_reimbursement, currency, effective_start_date, effective_end_date
                ) VALUES %s
                """,
                data["drg_costs"],
                page_size=500,
            )

            execute_values(
                cur,
                """
                INSERT INTO fin_billing.care_programs (
                    care_program_id, program_code, program_name, description,
                    target_risk_tier, intervention_cost, currency, duration_days,
                    monthly_capacity_slots, staff_hours_per_patient,
                    expected_readmission_reduction, effectiveness_ci_low,
                    effectiveness_ci_high, is_active, effective_start_date, effective_end_date
                ) VALUES %s
                """,
                data["care_programs"],
                page_size=500,
            )

            execute_values(
                cur,
                """
                INSERT INTO ehr_clinical.patients (
                    patient_id, mrn, first_name, last_name, birth_date, sex,
                    postal_code, payer_type, lives_alone, nursing_care_level,
                    distance_to_hospital_km, has_primary_care_physician,
                    is_deceased, deceased_date
                ) VALUES %s
                """,
                data["patients"],
                page_size=2000,
            )

            execute_values(
                cur,
                """
                INSERT INTO ehr_clinical.encounters (
                    encounter_id, patient_id, previous_encounter_id,
                    encounter_number, encounter_type, admit_datetime,
                    discharge_datetime, admission_source, discharge_disposition,
                    department, primary_diagnosis_code, primary_diagnosis_desc,
                    secondary_diagnosis_codes, charlson_score_at_admit,
                    attending_physician_id
                ) VALUES %s
                """,
                data["encounters"],
                page_size=2000,
            )

            execute_values(
                cur,
                """
                INSERT INTO ehr_clinical.vital_signs (
                    encounter_id, patient_id, recorded_datetime,
                    heart_rate_bpm, systolic_bp_mmhg, diastolic_bp_mmhg,
                    respiratory_rate_rpm, spo2_pct, temperature_c, news2_score
                ) VALUES %s
                """,
                data["vital_signs"],
                page_size=5000,
            )

            execute_values(
                cur,
                """
                INSERT INTO ehr_clinical.lab_results (
                    encounter_id, patient_id, loinc_code, test_name,
                    test_category, result_value_num, result_value_text,
                    unit, reference_range_low, reference_range_high,
                    abnormal_flag, collected_datetime, resulted_datetime
                ) VALUES %s
                """,
                data["lab_results"],
                page_size=5000,
            )

            execute_values(
                cur,
                """
                INSERT INTO ehr_clinical.medications (
                    encounter_id, patient_id, rxnorm_code, medication_name,
                    therapeutic_class, dosage, route, frequency, order_type,
                    is_high_risk, start_datetime, end_datetime,
                    prescribing_physician_id
                ) VALUES %s
                """,
                data["medications"],
                page_size=5000,
            )

            execute_values(
                cur,
                """
                INSERT INTO fin_billing.encounter_billing (
                    encounter_id, patient_id, drg_code, care_program_id,
                    payer_name, payer_type, claim_status, currency,
                    total_charges, total_cost, expected_reimbursement,
                    paid_amount, is_case_consolidated, drg_consolidation_loss,
                    billing_date, paid_date
                ) VALUES %s
                """,
                data["encounter_billing"],
                page_size=2000,
            )

        conn.execute(text("""
            SELECT setval(pg_get_serial_sequence('ehr_clinical.patients', 'patient_id'),
                          COALESCE((SELECT MAX(patient_id) FROM ehr_clinical.patients), 1));
            SELECT setval(pg_get_serial_sequence('ehr_clinical.encounters', 'encounter_id'),
                          COALESCE((SELECT MAX(encounter_id) FROM ehr_clinical.encounters), 1));
            SELECT setval(pg_get_serial_sequence('fin_billing.care_programs', 'care_program_id'),
                          COALESCE((SELECT MAX(care_program_id) FROM fin_billing.care_programs), 1));
        """))

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("ANALYZE;"))

    engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Popula o banco hospitalar Project Aegis (padrão DACH).")
    parser.add_argument("--encounters", type=int, default=TARGET_ENCOUNTERS, help="Número de internações a gerar.")
    parser.add_argument("--seed", type=int, default=SEED, help="Seed aleatória para reprodutibilidade.")
    parser.add_argument("--no-reset", action="store_true", help="Não executa TRUNCATE antes de inserir.")
    args = parser.parse_args()

    t0 = time.perf_counter()
    print(f"[1/2] Gerando {args.encounters:,} internações DACH realistas (seed={args.seed})...")
    data = generate_dataset(target_encounters=args.encounters, seed=args.seed)
    t1 = time.perf_counter()
    print(
        f"      Gerado em {t1 - t0:.2f}s | "
        f"patients={len(data['patients']):,}, "
        f"encounters={len(data['encounters']):,}, "
        f"vital_signs={len(data['vital_signs']):,}, "
        f"lab_results={len(data['lab_results']):,}, "
        f"medications={len(data['medications']):,}, "
        f"billing={len(data['encounter_billing']):,}"
    )

    print("[2/2] Inserindo dados no PostgreSQL via SQLAlchemy + psycopg2...")
    load_into_postgres(data, reset=not args.no_reset)
    t2 = time.perf_counter()
    print(f"      Carga concluída e ANALYZE executado em {t2 - t1:.2f}s (total: {t2 - t0:.2f}s).")


if __name__ == "__main__":
    main()

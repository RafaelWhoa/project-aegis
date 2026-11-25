-- =============================================================================
-- Project Aegis (Healthcare Risk & ROI Engine - DACH Market)
-- Script: sql/02_feature_extraction.sql
-- Descrição: Cria a View analytics.abt_readmission_features livre de data
--            leakage no momento da alta hospitalar, unindo ehr_clinical e
--            fin_billing com Window Functions para histórico de 6 meses,
--            últimos exames liberados antes da alta e últimos sinais vitais.
--
-- Convenção de colunas para o pipeline de ML:
--   - Identificadores / Split : encounter_id, patient_id, admit_datetime,
--                               discharge_datetime, recommended_split
--   - Features na alta        : todas as colunas clínicas, SDOH, vitais, labs,
--                               medicações e parâmetros DRG de admissão/alta
--   - Avaliação financeira    : prefixo eval_* (NÃO usar como feature no X_train!)
--   - Target                  : target_readmitted_30d, target_days_to_readmission
-- =============================================================================

CREATE OR REPLACE VIEW analytics.abt_readmission_features AS
WITH dataset_bounds AS (
    SELECT
        MIN(admit_datetime) AS min_admit_datetime,
        MAX(admit_datetime) AS max_admit_datetime
    FROM ehr_clinical.encounters
),

-- 1. Histórico longitudinal de internações via Window Functions
encounter_history AS (
    SELECT
        e.encounter_id,
        e.patient_id,
        e.encounter_number,
        e.encounter_type,
        e.admit_datetime,
        e.discharge_datetime,
        e.admission_source,
        e.discharge_disposition,
        e.department,
        e.primary_diagnosis_code,
        COALESCE(cardinality(e.secondary_diagnosis_codes), 0) AS secondary_diagnosis_count,
        e.charlson_score_at_admit,
        ROUND(
            (EXTRACT(EPOCH FROM (e.discharge_datetime - e.admit_datetime)) / 86400.0)::NUMERIC,
            2
        ) AS length_of_stay_days,

        -- Window Function (RANGE temporal): nº de internações nos últimos 6 meses (180 dias)
        COUNT(*) OVER (
            PARTITION BY e.patient_id
            ORDER BY e.admit_datetime
            RANGE BETWEEN INTERVAL '180 days' PRECEDING AND INTERVAL '1 second' PRECEDING
        )::INTEGER AS admissions_last_6m,

        -- Total acumulado de internações anteriores do paciente
        (ROW_NUMBER() OVER (
            PARTITION BY e.patient_id
            ORDER BY e.admit_datetime, e.encounter_id
        ) - 1)::INTEGER AS prior_admissions_total,

        -- Dias desde a alta da internação imediatamente anterior
        ROUND(
            (EXTRACT(EPOCH FROM (
                e.admit_datetime - LAG(e.discharge_datetime) OVER (
                    PARTITION BY e.patient_id
                    ORDER BY e.admit_datetime, e.encounter_id
                )
            )) / 86400.0)::NUMERIC,
            2
        ) AS days_since_last_discharge,

        -- Próxima admissão e custo real dela (usados apenas para Target e eval_*)
        LEAD(e.admit_datetime) OVER (
            PARTITION BY e.patient_id
            ORDER BY e.admit_datetime, e.encounter_id
        ) AS next_admit_datetime,
        LEAD(e.encounter_id) OVER (
            PARTITION BY e.patient_id
            ORDER BY e.admit_datetime, e.encounter_id
        ) AS next_encounter_id
    FROM ehr_clinical.encounters AS e
    WHERE e.discharge_datetime IS NOT NULL
),

-- 2. Exames laboratoriais: separação estrita entre liberados até a alta vs pendentes na alta
ranked_resulted_labs AS (
    SELECT
        lr.encounter_id,
        lr.lab_result_id,
        lr.loinc_code,
        lr.test_name,
        lr.result_value_num,
        lr.abnormal_flag,
        lr.collected_datetime,
        lr.resulted_datetime,
        e.discharge_datetime,

        -- Último exame liberado antes da alta (geral)
        ROW_NUMBER() OVER (
            PARTITION BY lr.encounter_id
            ORDER BY lr.collected_datetime DESC, lr.lab_result_id DESC
        ) AS rn_overall_last,

        -- Último exame liberado antes da alta por biomarcador (LOINC)
        ROW_NUMBER() OVER (
            PARTITION BY lr.encounter_id, lr.loinc_code
            ORDER BY lr.collected_datetime DESC, lr.lab_result_id DESC
        ) AS rn_loinc_last
    FROM ehr_clinical.lab_results AS lr
    INNER JOIN ehr_clinical.encounters AS e
        ON e.encounter_id = lr.encounter_id
       AND e.patient_id   = lr.patient_id
    WHERE e.discharge_datetime IS NOT NULL
      AND lr.collected_datetime <= e.discharge_datetime
      -- Regra anti-leakage: apenas resultados efetivamente liberados antes da alta
      AND lr.resulted_datetime  <= e.discharge_datetime
),

pending_labs_summary AS (
    SELECT
        lr.encounter_id,
        COUNT(*)::INTEGER AS pending_labs_at_discharge_count
    FROM ehr_clinical.lab_results AS lr
    INNER JOIN ehr_clinical.encounters AS e
        ON e.encounter_id = lr.encounter_id
       AND e.patient_id   = lr.patient_id
    WHERE e.discharge_datetime IS NOT NULL
      AND lr.collected_datetime <= e.discharge_datetime
      AND (lr.resulted_datetime IS NULL OR lr.resulted_datetime > e.discharge_datetime)
    GROUP BY lr.encounter_id
),

last_labs_before_discharge AS (
    SELECT
        encounter_id,
        MAX(test_name)        FILTER (WHERE rn_overall_last = 1) AS last_lab_test_name,
        MAX(loinc_code)       FILTER (WHERE rn_overall_last = 1) AS last_lab_loinc_code,
        MAX(result_value_num) FILTER (WHERE rn_overall_last = 1) AS last_lab_value_num,
        MAX(abnormal_flag)    FILTER (WHERE rn_overall_last = 1) AS last_lab_abnormal_flag,
        ROUND(
            (MAX(EXTRACT(EPOCH FROM (discharge_datetime - collected_datetime)) / 3600.0)
             FILTER (WHERE rn_overall_last = 1))::NUMERIC,
            2
        ) AS hours_last_lab_to_discharge,

        -- Último valor liberado antes da alta por biomarcador clínico (LOINC)
        MAX(result_value_num) FILTER (WHERE loinc_code = '718-7'   AND rn_loinc_last = 1) AS last_hemoglobin,
        MAX(result_value_num) FILTER (WHERE loinc_code = '2951-2'  AND rn_loinc_last = 1) AS last_sodium,
        MAX(result_value_num) FILTER (WHERE loinc_code = '2160-0'  AND rn_loinc_last = 1) AS last_creatinine,
        MAX(result_value_num) FILTER (WHERE loinc_code = '6690-2'  AND rn_loinc_last = 1) AS last_wbc,
        MAX(result_value_num) FILTER (WHERE loinc_code = '2345-7'  AND rn_loinc_last = 1) AS last_glucose,
        MAX(result_value_num) FILTER (WHERE loinc_code = '2823-3'  AND rn_loinc_last = 1) AS last_potassium,
        MAX(result_value_num) FILTER (WHERE loinc_code = '30934-4' AND rn_loinc_last = 1) AS last_bnp,

        COUNT(*) FILTER (
            WHERE rn_loinc_last = 1
              AND abnormal_flag <> 'NORMAL'
        )::INTEGER AS abnormal_last_labs_count,

        COUNT(*) FILTER (
            WHERE collected_datetime >= discharge_datetime - INTERVAL '48 hours'
              AND abnormal_flag <> 'NORMAL'
        )::INTEGER AS abnormal_labs_last_48h_count,

        COUNT(*) FILTER (
            WHERE collected_datetime >= discharge_datetime - INTERVAL '48 hours'
              AND abnormal_flag IN ('CRITICAL_LOW', 'CRITICAL_HIGH')
        )::INTEGER AS critical_labs_last_48h_count
    FROM ranked_resulted_labs
    GROUP BY encounter_id
),

-- 3. Últimos sinais vitais antes da alta via Window Function (NEWS2 e instabilidade 24h)
ranked_vitals AS (
    SELECT
        vs.encounter_id,
        vs.heart_rate_bpm,
        vs.systolic_bp_mmhg,
        vs.diastolic_bp_mmhg,
        vs.respiratory_rate_rpm,
        vs.spo2_pct,
        vs.temperature_c,
        vs.news2_score,
        vs.recorded_datetime,
        e.discharge_datetime,
        ROW_NUMBER() OVER (
            PARTITION BY vs.encounter_id
            ORDER BY vs.recorded_datetime DESC, vs.vital_sign_id DESC
        ) AS rn_last_vital
    FROM ehr_clinical.vital_signs AS vs
    INNER JOIN ehr_clinical.encounters AS e
        ON e.encounter_id = vs.encounter_id
       AND e.patient_id   = vs.patient_id
    WHERE e.discharge_datetime IS NOT NULL
      AND vs.recorded_datetime <= e.discharge_datetime
),

last_vitals_before_discharge AS (
    SELECT
        encounter_id,
        MAX(heart_rate_bpm)       FILTER (WHERE rn_last_vital = 1) AS last_heart_rate_bpm,
        MAX(systolic_bp_mmhg)     FILTER (WHERE rn_last_vital = 1) AS last_systolic_bp_mmhg,
        MAX(diastolic_bp_mmhg)    FILTER (WHERE rn_last_vital = 1) AS last_diastolic_bp_mmhg,
        MAX(respiratory_rate_rpm) FILTER (WHERE rn_last_vital = 1) AS last_respiratory_rate_rpm,
        MAX(spo2_pct)             FILTER (WHERE rn_last_vital = 1) AS last_spo2_pct,
        MAX(temperature_c)        FILTER (WHERE rn_last_vital = 1) AS last_temperature_c,
        MAX(news2_score)          FILTER (WHERE rn_last_vital = 1) AS last_news2_score,
        COUNT(*) FILTER (
            WHERE recorded_datetime >= discharge_datetime - INTERVAL '24 hours'
              AND news2_score >= 5
        )::INTEGER AS vital_instability_last_24h_count
    FROM ranked_vitals
    GROUP BY encounter_id
),

-- 4. Sumário de medicações prescritas até a alta
med_summary AS (
    SELECT
        m.encounter_id,
        COUNT(*)::INTEGER AS total_medications_count,
        COUNT(*) FILTER (WHERE m.order_type = 'DISCHARGE')::INTEGER AS discharge_medications_count,
        COUNT(*) FILTER (WHERE m.is_high_risk IS TRUE)::INTEGER AS high_risk_medications_count,
        BOOL_OR(m.is_high_risk AND m.order_type = 'DISCHARGE') AS has_high_risk_discharge_med
    FROM ehr_clinical.medications AS m
    GROUP BY m.encounter_id
)

-- 5. Tabela Analítica Final (ABT) pronta para treino e avaliação financeira
SELECT
    -- Identificadores e split temporal sugerido (out-of-time: últimos 6 meses elegíveis = test)
    eh.encounter_id,
    eh.patient_id,
    eh.admit_datetime,
    eh.discharge_datetime,
    CASE
        WHEN eh.discharge_datetime >= db.max_admit_datetime - INTERVAL '210 days'
        THEN 'test'
        ELSE 'train'
    END AS recommended_split,

    -- Demografia e SDOH (ehr_clinical.patients - sem is_deceased!)
    DATE_PART('year', AGE(eh.admit_datetime::DATE, p.birth_date))::INTEGER AS age_at_admission,
    p.sex,
    p.payer_type,
    p.lives_alone,
    p.nursing_care_level,
    p.distance_to_hospital_km,
    p.has_primary_care_physician,

    -- Características clínicas da internação (ehr_clinical.encounters)
    eh.charlson_score_at_admit,
    eh.encounter_type,
    eh.admission_source,
    eh.discharge_disposition,
    eh.department,
    eh.primary_diagnosis_code,
    eh.secondary_diagnosis_count,
    eh.length_of_stay_days,

    -- Histórico de internações via Window Functions
    eh.admissions_last_6m,
    eh.prior_admissions_total,
    eh.days_since_last_discharge,

    -- Últimos sinais vitais pré-alta (ehr_clinical.vital_signs)
    lv.last_heart_rate_bpm,
    lv.last_systolic_bp_mmhg,
    lv.last_diastolic_bp_mmhg,
    lv.last_respiratory_rate_rpm,
    lv.last_spo2_pct,
    lv.last_temperature_c,
    lv.last_news2_score,
    COALESCE(lv.vital_instability_last_24h_count, 0) AS vital_instability_last_24h_count,

    -- Último exame laboratorial liberado antes da alta e biomarcadores pré-alta
    ll.last_lab_test_name,
    ll.last_lab_loinc_code,
    ll.last_lab_value_num,
    ll.last_lab_abnormal_flag,
    ll.hours_last_lab_to_discharge,
    ll.last_hemoglobin,
    ll.last_sodium,
    ll.last_creatinine,
    ll.last_wbc,
    ll.last_glucose,
    ll.last_potassium,
    ll.last_bnp,
    COALESCE(ll.abnormal_last_labs_count, 0)         AS abnormal_last_labs_count,
    COALESCE(ll.abnormal_labs_last_48h_count, 0)     AS abnormal_labs_last_48h_count,
    COALESCE(ll.critical_labs_last_48h_count, 0)     AS critical_labs_last_48h_count,
    COALESCE(pl.pending_labs_at_discharge_count, 0)  AS pending_labs_at_discharge_count,

    -- Perfil medicamentoso (ehr_clinical.medications)
    COALESCE(ms.total_medications_count, 0)          AS total_medications_count,
    COALESCE(ms.discharge_medications_count, 0)      AS discharge_medications_count,
    COALESCE(ms.high_risk_medications_count, 0)      AS high_risk_medications_count,
    COALESCE(ms.has_high_risk_discharge_med, FALSE)  AS has_high_risk_discharge_med,

    -- Parâmetros DRG conhecidos até a alta (fin_billing.drg_costs - SEM custos pós-alta)
    eb.drg_code                                      AS working_drg_code,
    dc.drg_type,
    dc.mdc_code,
    dc.relative_weight                               AS drg_relative_weight,
    dc.base_rate                                     AS drg_base_rate,
    dc.geometric_mean_los                            AS drg_geometric_mean_los,
    dc.ogvd_days                                     AS drg_ogvd_days,
    dc.standard_cost                                 AS drg_standard_cost,
    ROUND(
        (eh.length_of_stay_days / NULLIF(dc.geometric_mean_los, 0))::NUMERIC,
        3
    ) AS los_to_gmlos_ratio,
    ROUND(
        (eh.length_of_stay_days / NULLIF(dc.ogvd_days, 0))::NUMERIC,
        3
    ) AS los_to_ogvd_ratio,

    -- Colunas de AVALIAÇÃO FINANCEIRA pós-alta (NÃO usar como feature no treino!)
    eb.total_cost                                    AS eval_index_actual_cost,
    dc.standard_cost                                 AS eval_estimated_readmission_cost,
    COALESCE(next_eb.drg_consolidation_loss, 0.00)   AS eval_realized_consolidation_loss,
    COALESCE(next_eb.total_cost, 0.00)               AS eval_realized_next_encounter_cost,

    -- Variável alvo (Target): reinternação em até 30 dias após a alta
    ROUND(
        (EXTRACT(EPOCH FROM (eh.next_admit_datetime - eh.discharge_datetime)) / 86400.0)::NUMERIC,
        2
    ) AS target_days_to_readmission,
    CASE
        WHEN eh.next_admit_datetime IS NOT NULL
         AND eh.next_admit_datetime <= eh.discharge_datetime + INTERVAL '30 days'
        THEN 1
        ELSE 0
    END AS target_readmitted_30d

FROM encounter_history AS eh
INNER JOIN ehr_clinical.patients AS p
    ON p.patient_id = eh.patient_id
INNER JOIN fin_billing.encounter_billing AS eb
    ON eb.encounter_id = eh.encounter_id
   AND eb.patient_id   = eh.patient_id
INNER JOIN fin_billing.drg_costs AS dc
    ON dc.drg_code = eb.drg_code
LEFT JOIN fin_billing.encounter_billing AS next_eb
    ON next_eb.encounter_id = eh.next_encounter_id
   AND eh.next_admit_datetime <= eh.discharge_datetime + INTERVAL '30 days'
LEFT JOIN last_labs_before_discharge AS ll
    ON ll.encounter_id = eh.encounter_id
LEFT JOIN pending_labs_summary AS pl
    ON pl.encounter_id = eh.encounter_id
LEFT JOIN last_vitals_before_discharge AS lv
    ON lv.encounter_id = eh.encounter_id
LEFT JOIN med_summary AS ms
    ON ms.encounter_id = eh.encounter_id
CROSS JOIN dataset_bounds AS db

-- Filtros de coorte de treinamento:
--   1. Exclui óbitos intra-hospitalares (não elegíveis à reinternação).
--   2. Exclui altas nos últimos 30 dias da janela (evita censura à direita).
WHERE eh.discharge_disposition <> 'EXPIRED'
  AND eh.discharge_datetime <= db.max_admit_datetime - INTERVAL '30 days';

COMMENT ON VIEW analytics.abt_readmission_features IS 'Analytical Base Table (ABT) livre de data leakage no momento da alta hospitalar para treinamento de modelos de reinternação em 30 dias.';

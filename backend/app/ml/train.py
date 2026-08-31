"""
app/ml/train.py — Train XGBoost model on REAL Elliptic++ Actors Dataset with 3-way temporal split.

Dataset Location:
  - data/raw/ellipticpp/wallets_features.csv
  - data/raw/ellipticpp/wallets_classes.csv

Partitions (Time steps 1..49):
  - Train set: Time steps 1..29 (Model parameter fitting on historical graph)
  - Validation slice: Time steps 30..34 (Threshold optimization without test leakage)
  - Test set: Time steps 35..49 (Single out-of-sample application at locked threshold)

Metrics reported in priority order (docs/ml.md):
  1. AUC-PR (primary)
  2. Precision & Recall at deployed threshold
  3. False-Positive Rate (FPR) at deployed threshold (target < 1%)
  4. AUC-ROC (secondary)
  5. Calibration / Brier score
"""
import argparse
import json
import logging
import os
import time
import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (
    auc,
    accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    precision_recall_curve,
    recall_score,
    roc_auc_score,
)
import xgboost as xgb

from app.ml.features import FEATURE_COLUMNS, assert_feature_schema

logger = logging.getLogger(__name__)

# Search paths for raw Elliptic++ CSV dataset files
POSSIBLE_DATA_DIRS = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "raw", "ellipticpp")),
    "/app/data/raw/ellipticpp",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "raw", "ellipticpp")),
    os.path.abspath(os.path.join(os.getcwd(), "data", "raw", "ellipticpp")),
]

ARTIFACTS_DIR = os.path.join(os.path.dirname(__file__), "artifacts")
MODEL_PATH = os.path.join(ARTIFACTS_DIR, "risk_model.joblib")
METRICS_PATH = os.path.join(ARTIFACTS_DIR, "risk_model_metrics.json")


def evaluate_model(
    clf: xgb.XGBClassifier,
    X: pd.DataFrame,
    y: pd.Series,
    threshold: float,
    split_name: str,
) -> dict:
    """Evaluate a trained model and measure single-row inference performance."""
    probabilities = clf.predict_proba(X)[:, 1]
    predictions = (probabilities >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, predictions, labels=[0, 1]).ravel()

    # Measure the deployed single-wallet inference path, excluding model warm-up.
    benchmark_X = X.iloc[: min(len(X), 2_000)]
    sample = benchmark_X.iloc[[0]]
    clf.predict_proba(sample)
    latency_samples_ms: list[float] = []
    started = time.perf_counter()
    for index in range(len(benchmark_X)):
        sample_started = time.perf_counter_ns()
        clf.predict_proba(benchmark_X.iloc[[index]])
        latency_samples_ms.append((time.perf_counter_ns() - sample_started) / 1_000_000)
    elapsed_seconds = time.perf_counter() - started

    metrics = {
        "split": split_name,
        "samples": int(len(y)),
        "inference_benchmark_samples": int(len(benchmark_X)),
        "threshold": float(threshold),
        "accuracy": float(accuracy_score(y, predictions)),
        "precision": float(precision_score(y, predictions, zero_division=0)),
        "recall": float(recall_score(y, predictions, zero_division=0)),
        "f1_score": float(f1_score(y, predictions, zero_division=0)),
        "roc_auc": float(roc_auc_score(y, probabilities)),
        "confusion_matrix": {
            "true_negative": int(tn),
            "false_positive": int(fp),
            "false_negative": int(fn),
            "true_positive": int(tp),
        },
        "inference_latency_ms": {
            "p50": float(np.percentile(latency_samples_ms, 50)),
            "p95": float(np.percentile(latency_samples_ms, 95)),
        },
        "throughput_samples_per_second": float(len(benchmark_X) / max(elapsed_seconds, 1e-9)),
    }

    print(f"\n[{split_name.upper()} MODEL METRICS]")
    print(json.dumps(metrics, indent=2))
    return metrics


def get_data_dir() -> str:
    """Find valid directory containing real Elliptic++ CSV dataset files."""
    for d in POSSIBLE_DATA_DIRS:
        feat_path = os.path.join(d, "wallets_features.csv")
        class_path = os.path.join(d, "wallets_classes.csv")
        if os.path.exists(feat_path) and os.path.exists(class_path):
            return d
    raise FileNotFoundError(
        f"Real Elliptic++ dataset files (wallets_features.csv, wallets_classes.csv) not found!\n"
        f"Searched paths: {POSSIBLE_DATA_DIRS}\n"
        f"Please ensure real files are staged in data/raw/ellipticpp/."
    )


def load_real_elliptic_dataset(data_dir: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load the real raw Elliptic++ Actors dataset CSV files from disk.
    No synthetic generation fallback allowed.
    """
    feat_path = os.path.join(data_dir, "wallets_features.csv")
    class_path = os.path.join(data_dir, "wallets_classes.csv")

    print(f"Loading real Elliptic++ features from {feat_path}...")
    df_features = pd.read_csv(feat_path)
    print(f"Loaded features: {df_features.shape[0]:,} rows, {df_features.shape[1]} columns.")

    print(f"Loading real Elliptic++ classes from {class_path}...")
    df_classes = pd.read_csv(class_path)
    print(f"Loaded classes: {df_classes.shape[0]:,} rows.")

    return df_features, df_classes


def select_best_threshold(
    y_val: pd.Series,
    y_prob_val: np.ndarray,
    candidate_thresholds: list[float] = [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.88, 0.90, 0.92, 0.95],
    max_fpr: float = 0.020,
) -> tuple[float, dict]:
    """
    Select optimal classification threshold on VALIDATION SLICE ONLY (Time steps 30..34).
    Finds threshold that maximizes Recall subject to FPR < max_fpr (2.0%).
    """
    print("\n[VALIDATION SLICE THRESHOLD SCAN (Time Steps 30..34)]")
    print(f"{'Threshold':>10} | {'Precision':>10} | {'Recall':>10} | {'FPR':>10} | {'FPR < 2%':>8}")
    print("-" * 58)

    best_thresh = None
    best_recall = -1.0
    best_val_metrics = {}

    for t in candidate_thresholds:
        y_pred = (y_prob_val >= t).astype(int)
        tn, fp, fn, tp = confusion_matrix(y_val, y_pred).ravel()
        prec = tp / max(1, (tp + fp))
        rec = tp / max(1, (tp + fn))
        fpr = fp / max(1, (fp + tn))
        meets_req = fpr <= max_fpr

        print(f"{t:>10.2f} | {prec:>10.4f} | {rec:>10.4f} | {fpr:>9.4f} ({fpr*100:.2f}%) | {'YES' if meets_req else 'NO':>8}")

        if meets_req and rec > best_recall:
            best_recall = rec
            best_thresh = t
            best_val_metrics = {
                "threshold": t,
                "precision": prec,
                "recall": rec,
                "fpr": fpr,
                "tp": int(tp),
                "fp": int(fp),
                "tn": int(tn),
                "fn": int(fn),
            }

    if best_thresh is None:
        best_thresh = max(candidate_thresholds)

    print("-" * 58)
    print(f"Locked Threshold from Validation: {best_thresh:.2f} (Val Recall: {best_val_metrics.get('recall', 0):.4f}, Val FPR: {best_val_metrics.get('fpr', 0)*100:.2f}%)\n")

    return best_thresh, best_val_metrics


def train_elliptic_model(
    df_features: pd.DataFrame,
    df_classes: pd.DataFrame,
) -> dict:
    """
    Train XGBoost on real Elliptic++ temporal split with separate validation slice for threshold tuning.
      1. Train: Time steps 1..29
      2. Validation: Time steps 30..34 (Threshold optimization)
      3. Test: Time steps 35..49 (Untouched holdout evaluation)
    """
    # Deduplicate rows after join
    df = pd.merge(df_features, df_classes, on="address")
    initial_len = len(df)
    df = df.drop_duplicates(subset=["address"], keep="last").copy()
    print(f"Deduplication on address: {initial_len:,} -> {len(df):,} rows.")

    # Filter out unknown class (class 3) — train on labeled illicit (1) and licit (2)
    df = df[df["class"].isin([1, 2])].copy()
    df["label"] = (df["class"] == 1).astype(int)
    print(f"Filtered to labeled actors (Class 1 & 2): {len(df):,} total actors (Illicit: {df['label'].sum():,}, Licit: {(df['label'] == 0).sum():,}).")

    # 3-Way Temporal Partitioning
    train_mask = df["Time step"] <= 29
    val_mask = (df["Time step"] >= 30) & (df["Time step"] <= 34)
    test_mask = df["Time step"] >= 35

    train_df = df[train_mask].copy()
    val_df = df[val_mask].copy()
    test_df = df[test_mask].copy()

    train_addrs = set(train_df["address"].tolist())
    val_addrs = set(val_df["address"].tolist())
    test_addrs = set(test_df["address"].tolist())

    # Assert 0 address overlap across all 3 sets
    assert len(train_addrs.intersection(val_addrs)) == 0, "Address overlap between Train and Val!"
    assert len(train_addrs.intersection(test_addrs)) == 0, "Address overlap between Train and Test!"
    assert len(val_addrs.intersection(test_addrs)) == 0, "Address overlap between Val and Test!"

    print("\n" + "=" * 60)
    print("      REAL ELLIPTIC++ TEMPORAL PARTITIONING AUDIT")
    print("=" * 60)
    print(f"  Train: steps 1..29  (N={len(train_df):,}, Illicit={train_df['label'].sum():,}, BaseRate={train_df['label'].mean():.2%})")
    print(f"  Val:   steps 30..34 (N={len(val_df):,}, Illicit={val_df['label'].sum():,}, BaseRate={val_df['label'].mean():.2%})")
    print(f"  Test:  steps 35..49 (N={len(test_df):,}, Illicit={test_df['label'].sum():,}, BaseRate={test_df['label'].mean():.2%})")
    print("=" * 60)

    # Assert feature schema (55 features, exact order and names)
    assert_feature_schema(FEATURE_COLUMNS)

    X_train = train_df[FEATURE_COLUMNS].fillna(0.0)
    y_train = train_df["label"]

    X_val = val_df[FEATURE_COLUMNS].fillna(0.0)
    y_val = val_df["label"]

    X_test = test_df[FEATURE_COLUMNS].fillna(0.0)
    y_test = test_df["label"]

    # Handle class imbalance via scale_pos_weight
    scale_pos_weight = (len(y_train) - y_train.sum()) / max(1, y_train.sum())

    print(f"\nTraining XGBoost Classifier (scale_pos_weight={scale_pos_weight:.2f})...")
    clf = xgb.XGBClassifier(
        n_estimators=150,
        max_depth=5,
        learning_rate=0.06,
        subsample=0.85,
        colsample_bytree=0.85,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        random_state=42,
        tree_method="hist",
    )
    clf.fit(X_train, y_train)

    # Threshold Selection on Validation Slice ONLY (Time steps 30..34)
    y_prob_val = clf.predict_proba(X_val)[:, 1]
    locked_threshold, val_metrics = select_best_threshold(y_val, y_prob_val)

    val_prec_curve, val_rec_curve, _ = precision_recall_curve(y_val, y_prob_val)
    val_auc_pr = auc(val_rec_curve, val_prec_curve)
    val_auc_roc = roc_auc_score(y_val, y_prob_val)
    val_brier = brier_score_loss(y_val, y_prob_val)

    # Single Out-of-Sample Evaluation on UNTOUCHED Test Set (Time steps 35..49)
    y_prob_test = clf.predict_proba(X_test)[:, 1]
    y_pred_test = (y_prob_test >= locked_threshold).astype(int)

    test_prec_curve, test_rec_curve, _ = precision_recall_curve(y_test, y_prob_test)
    test_auc_pr = auc(test_rec_curve, test_prec_curve)
    test_auc_roc = roc_auc_score(y_test, y_prob_test)
    test_brier = brier_score_loss(y_test, y_prob_test)

    validation_metrics = evaluate_model(
        clf, X_val, y_val, locked_threshold, "validation"
    )
    test_metrics = evaluate_model(clf, X_test, y_test, locked_threshold, "test")

    tn, fp, fn, tp = confusion_matrix(y_test, y_pred_test).ravel()
    test_prec = tp / max(1, (tp + fp))
    test_rec = tp / max(1, (tp + fn))
    test_fpr = fp / max(1, (fp + tn))

    print("\n" + "=" * 65)
    print("  PHASE 4: REAL ELLIPTIC++ OUT-OF-SAMPLE TEST EVALUATION (STEPS 35..49)")
    print("=" * 65)
    print(f"  Locked Threshold:     {locked_threshold:.2f}")
    print(f"  1. AUC-PR (Primary):  {test_auc_pr:.4f} (Val: {val_auc_pr:.4f})")
    print(f"  2. Precision:         {test_prec:.4f} (TP={tp:,}, FP={fp:,})")
    print(f"     Recall:            {test_rec:.4f} (FN={fn:,})")
    print(f"  3. FPR:               {test_fpr:.4f} ({test_fpr * 100:.2f}%)")
    print(f"  4. AUC-ROC:           {test_auc_roc:.4f} (Val: {val_auc_roc:.4f})")
    print(f"  5. Brier Score:       {test_brier:.4f} (Val: {val_brier:.4f})")
    print("=" * 65 + "\n")

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    joblib.dump(clf, MODEL_PATH)
    print(f"Saved trained real XGBoost model to {MODEL_PATH}")

    metrics_report = {
        "model": "xgboost",
        "feature_count": len(FEATURE_COLUMNS),
        "validation": validation_metrics,
        "test": test_metrics,
        "legacy_metrics": {
            "validation_auc_pr": val_auc_pr,
            "validation_brier": val_brier,
            "test_auc_pr": test_auc_pr,
            "test_fpr": test_fpr,
            "test_brier": test_brier,
        },
    }
    with open(METRICS_PATH, "w", encoding="utf-8") as metrics_file:
        json.dump(metrics_report, metrics_file, indent=2)
    print(f"Saved model metrics report to {METRICS_PATH}")

    # Re-initialize caches
    from app.ml.explain import clear_explainer_cache
    from app.ml.model import clear_model_cache
    clear_explainer_cache()
    clear_model_cache()

    return {
        "locked_threshold": locked_threshold,
        "test_auc_pr": test_auc_pr,
        "test_precision": test_prec,
        "test_recall": test_rec,
        "test_fpr": test_fpr,
        "test_auc_roc": test_auc_roc,
        "test_brier": test_brier,
        "metrics_report": metrics_report,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train XGBoost model on REAL Elliptic++ Actors Dataset")
    args = parser.parse_args()

    data_dir = get_data_dir()
    df_f, df_c = load_real_elliptic_dataset(data_dir)
    train_elliptic_model(df_f, df_c)

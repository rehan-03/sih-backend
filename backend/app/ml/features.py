"""
app/ml/features.py — Feature engineering for wallet risk scoring (Phase 4).

Dataset: Elliptic++ Actors Dataset schema (55 numeric features).
Extracts graph topology, volume, temporal, and counterparty features from live blockchain transactions.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional
import numpy as np

if TYPE_CHECKING:
    from app.services.explorers.base import RawTx

logger = logging.getLogger(__name__)

# ── Feature schema — 55 numeric features from Elliptic++ wallets_features.csv ──
FEATURE_COLUMNS: list[str] = [
    "num_txs_as_sender",
    "num_txs_as receiver",
    "first_block_appeared_in",
    "last_block_appeared_in",
    "lifetime_in_blocks",
    "total_txs",
    "first_sent_block",
    "first_received_block",
    "num_timesteps_appeared_in",
    "btc_transacted_total",
    "btc_transacted_min",
    "btc_transacted_max",
    "btc_transacted_mean",
    "btc_transacted_median",
    "btc_sent_total",
    "btc_sent_min",
    "btc_sent_max",
    "btc_sent_mean",
    "btc_sent_median",
    "btc_received_total",
    "btc_received_min",
    "btc_received_max",
    "btc_received_mean",
    "btc_received_median",
    "fees_total",
    "fees_min",
    "fees_max",
    "fees_mean",
    "fees_median",
    "fees_as_share_total",
    "fees_as_share_min",
    "fees_as_share_max",
    "fees_as_share_mean",
    "fees_as_share_median",
    "blocks_btwn_txs_total",
    "blocks_btwn_txs_min",
    "blocks_btwn_txs_max",
    "blocks_btwn_txs_mean",
    "blocks_btwn_txs_median",
    "blocks_btwn_input_txs_total",
    "blocks_btwn_input_txs_min",
    "blocks_btwn_input_txs_max",
    "blocks_btwn_input_txs_mean",
    "blocks_btwn_input_txs_median",
    "blocks_btwn_output_txs_total",
    "blocks_btwn_output_txs_min",
    "blocks_btwn_output_txs_max",
    "blocks_btwn_output_txs_mean",
    "blocks_btwn_output_txs_median",
    "num_addr_transacted_multiple",
    "transacted_w_address_total",
    "transacted_w_address_min",
    "transacted_w_address_max",
    "transacted_w_address_mean",
    "transacted_w_address_median",
]


def assert_feature_schema(computed_columns: list[str]) -> None:
    """
    Guard that prevents train/inference feature mismatch.
    Call this before every training run and before wiring the model into /risk.
    Raises AssertionError if the schemas don't match exactly.
    """
    assert computed_columns == FEATURE_COLUMNS, (
        f"Feature schema mismatch!\n"
        f"Expected: {FEATURE_COLUMNS}\n"
        f"Got:      {computed_columns}"
    )


def extract_features_from_transactions(
    address: str,
    chain: str,
    txs: List[RawTx],
    current_block: int = 860000,
) -> dict[str, float]:
    """
    Extract the 55 Elliptic++ feature schema features dynamically from a list of raw transactions.
    """
    sent_txs = [t for t in txs if t.from_address.lower() == address.lower()]
    recv_txs = [t for t in txs if t.to_address.lower() == address.lower()]
    # Sanitize and cap transaction amounts
    all_amounts = [min(1000000.0, max(0.0, float(t.amount))) for t in txs if 0 <= float(t.amount) < 1e15] if txs else [0.0]
    sent_amounts = [min(1000000.0, max(0.0, float(t.amount))) for t in sent_txs if 0 <= float(t.amount) < 1e15] if sent_txs else [0.0]
    recv_amounts = [min(1000000.0, max(0.0, float(t.amount))) for t in recv_txs if 0 <= float(t.amount) < 1e15] if recv_txs else [0.0]

    num_sent = float(len(sent_txs))
    num_recv = float(len(recv_txs))
    total_txs = float(len(txs))

    # Amounts statistics
    btc_transacted_total = float(sum(all_amounts))
    btc_transacted_min = float(min(all_amounts)) if all_amounts else 0.0
    btc_transacted_max = float(max(all_amounts)) if all_amounts else 0.0
    btc_transacted_mean = float(np.mean(all_amounts)) if all_amounts else 0.0
    btc_transacted_median = float(np.median(all_amounts)) if all_amounts else 0.0

    btc_sent_total = float(sum(sent_amounts))
    btc_sent_min = float(min(sent_amounts)) if sent_amounts else 0.0
    btc_sent_max = float(max(sent_amounts)) if sent_amounts else 0.0
    btc_sent_mean = float(np.mean(sent_amounts)) if sent_amounts else 0.0
    btc_sent_median = float(np.median(sent_amounts)) if sent_amounts else 0.0

    btc_received_total = float(sum(recv_amounts))
    btc_received_min = float(min(recv_amounts)) if recv_amounts else 0.0
    btc_received_max = float(max(recv_amounts)) if recv_amounts else 0.0
    btc_received_mean = float(np.mean(recv_amounts)) if recv_amounts else 0.0
    btc_received_median = float(np.median(recv_amounts)) if recv_amounts else 0.0

    # Dynamic block calculation (BTC Genesis: Jan 3 2009 1230950400, ~600s/block)
    GENESIS_TS = 1230950400.0

    def block_from_ts(ts: datetime) -> float:
        try:
            return max(0.0, float((ts.timestamp() - GENESIS_TS) / 600.0))
        except Exception:
            return float(current_block)

    if txs:
        all_blocks = [block_from_ts(t.timestamp) for t in txs]
        first_block = float(min(all_blocks))
        last_block = float(max(all_blocks))
        lifetime_in_blocks = max(0.0, float(last_block - first_block))
    else:
        first_block = float(current_block)
        last_block = float(current_block)
        lifetime_in_blocks = 0.0

    sent_blocks = [block_from_ts(t.timestamp) for t in sent_txs] if sent_txs else []
    recv_blocks = [block_from_ts(t.timestamp) for t in recv_txs] if recv_txs else []
    first_sent_block = float(min(sent_blocks)) if sent_blocks else 0.0
    first_received_block = float(min(recv_blocks)) if recv_blocks else 0.0

    # Dynamic consecutive transaction intervals (sorted chronologically)
    sorted_txs = sorted(txs, key=lambda t: t.timestamp)
    if len(sorted_txs) > 1:
        tx_diffs = [
            max(0.0, (sorted_txs[i + 1].timestamp - sorted_txs[i].timestamp).total_seconds() / 600.0)
            for i in range(len(sorted_txs) - 1)
        ]
        blocks_btwn_txs_total = float(sum(tx_diffs))
        blocks_btwn_txs_min = float(min(tx_diffs))
        blocks_btwn_txs_max = float(max(tx_diffs))
        blocks_btwn_txs_mean = float(np.mean(tx_diffs))
        blocks_btwn_txs_median = float(np.median(tx_diffs))
    else:
        blocks_btwn_txs_total = 0.0
        blocks_btwn_txs_min = 0.0
        blocks_btwn_txs_max = 0.0
        blocks_btwn_txs_mean = 0.0
        blocks_btwn_txs_median = 0.0

    sorted_sent = sorted(sent_txs, key=lambda t: t.timestamp)
    if len(sorted_sent) > 1:
        sent_diffs = [
            max(0.0, (sorted_sent[i + 1].timestamp - sorted_sent[i].timestamp).total_seconds() / 600.0)
            for i in range(len(sorted_sent) - 1)
        ]
        blocks_btwn_input_txs_total = float(sum(sent_diffs))
        blocks_btwn_input_txs_min = float(min(sent_diffs))
        blocks_btwn_input_txs_max = float(max(sent_diffs))
        blocks_btwn_input_txs_mean = float(np.mean(sent_diffs))
        blocks_btwn_input_txs_median = float(np.median(sent_diffs))
    else:
        blocks_btwn_input_txs_total = 0.0
        blocks_btwn_input_txs_min = 0.0
        blocks_btwn_input_txs_max = 0.0
        blocks_btwn_input_txs_mean = 0.0
        blocks_btwn_input_txs_median = 0.0

    sorted_recv = sorted(recv_txs, key=lambda t: t.timestamp)
    if len(sorted_recv) > 1:
        recv_diffs = [
            max(0.0, (sorted_recv[i + 1].timestamp - sorted_recv[i].timestamp).total_seconds() / 600.0)
            for i in range(len(sorted_recv) - 1)
        ]
        blocks_btwn_output_txs_total = float(sum(recv_diffs))
        blocks_btwn_output_txs_min = float(min(recv_diffs))
        blocks_btwn_output_txs_max = float(max(recv_diffs))
        blocks_btwn_output_txs_mean = float(np.mean(recv_diffs))
        blocks_btwn_output_txs_median = float(np.median(recv_diffs))
    else:
        blocks_btwn_output_txs_total = 0.0
        blocks_btwn_output_txs_min = 0.0
        blocks_btwn_output_txs_max = 0.0
        blocks_btwn_output_txs_mean = 0.0
        blocks_btwn_output_txs_median = 0.0

    # Counterparty addresses & frequencies
    counterparty_counts: dict[str, int] = {}
    for t in sent_txs:
        c_addr = t.to_address.lower()
        counterparty_counts[c_addr] = counterparty_counts.get(c_addr, 0) + 1
    for t in recv_txs:
        c_addr = t.from_address.lower()
        counterparty_counts[c_addr] = counterparty_counts.get(c_addr, 0) + 1

    unique_counterparties = float(len(counterparty_counts))
    counts_list = list(counterparty_counts.values()) if counterparty_counts else [0]
    num_addr_multiple = float(sum(1 for c in counts_list if c > 1))

    transacted_w_address_total = unique_counterparties
    transacted_w_address_min = float(min(counts_list)) if counterparty_counts else 0.0
    transacted_w_address_max = float(max(counts_list)) if counterparty_counts else 0.0
    transacted_w_address_mean = float(np.mean(counts_list)) if counterparty_counts else 0.0
    transacted_w_address_median = float(np.median(counts_list)) if counterparty_counts else 0.0

    fees_total = round(btc_transacted_total * 0.0001, 6)
    fees_mean = round(fees_total / max(1.0, total_txs), 6)
    fees_min = round(fees_mean * 0.5, 6)
    fees_max = round(fees_mean * 2.0, 6)
    fees_median = fees_mean

    fees_as_share = 0.0001
    fees_as_share_total = fees_as_share
    fees_as_share_min = fees_as_share * 0.5
    fees_as_share_max = fees_as_share * 2.0
    fees_as_share_mean = fees_as_share
    fees_as_share_median = fees_as_share

    timesteps_appeared = float(min(49.0, max(1.0, (lifetime_in_blocks / 2016.0) + 1.0)))

    feature_dict = {
        "num_txs_as_sender": num_sent,
        "num_txs_as receiver": num_recv,
        "first_block_appeared_in": first_block,
        "last_block_appeared_in": last_block,
        "lifetime_in_blocks": lifetime_in_blocks,
        "total_txs": total_txs,
        "first_sent_block": first_sent_block,
        "first_received_block": first_received_block,
        "num_timesteps_appeared_in": timesteps_appeared,
        "btc_transacted_total": btc_transacted_total,
        "btc_transacted_min": btc_transacted_min,
        "btc_transacted_max": btc_transacted_max,
        "btc_transacted_mean": btc_transacted_mean,
        "btc_transacted_median": btc_transacted_median,
        "btc_sent_total": btc_sent_total,
        "btc_sent_min": btc_sent_min,
        "btc_sent_max": btc_sent_max,
        "btc_sent_mean": btc_sent_mean,
        "btc_sent_median": btc_sent_median,
        "btc_received_total": btc_received_total,
        "btc_received_min": btc_received_min,
        "btc_received_max": btc_received_max,
        "btc_received_mean": btc_received_mean,
        "btc_received_median": btc_received_median,
        "fees_total": fees_total,
        "fees_min": fees_min,
        "fees_max": fees_max,
        "fees_mean": fees_mean,
        "fees_median": fees_median,
        "fees_as_share_total": fees_as_share_total,
        "fees_as_share_min": fees_as_share_min,
        "fees_as_share_max": fees_as_share_max,
        "fees_as_share_mean": fees_as_share_mean,
        "fees_as_share_median": fees_as_share_median,
        "blocks_btwn_txs_total": blocks_btwn_txs_total,
        "blocks_btwn_txs_min": blocks_btwn_txs_min,
        "blocks_btwn_txs_max": blocks_btwn_txs_max,
        "blocks_btwn_txs_mean": blocks_btwn_txs_mean,
        "blocks_btwn_txs_median": blocks_btwn_txs_median,
        "blocks_btwn_input_txs_total": blocks_btwn_input_txs_total,
        "blocks_btwn_input_txs_min": blocks_btwn_input_txs_min,
        "blocks_btwn_input_txs_max": blocks_btwn_input_txs_max,
        "blocks_btwn_input_txs_mean": blocks_btwn_input_txs_mean,
        "blocks_btwn_input_txs_median": blocks_btwn_input_txs_median,
        "blocks_btwn_output_txs_total": blocks_btwn_output_txs_total,
        "blocks_btwn_output_txs_min": blocks_btwn_output_txs_min,
        "blocks_btwn_output_txs_max": blocks_btwn_output_txs_max,
        "blocks_btwn_output_txs_mean": blocks_btwn_output_txs_mean,
        "blocks_btwn_output_txs_median": blocks_btwn_output_txs_median,
        "num_addr_transacted_multiple": num_addr_multiple,
        "transacted_w_address_total": transacted_w_address_total,
        "transacted_w_address_min": transacted_w_address_min,
        "transacted_w_address_max": transacted_w_address_max,
        "transacted_w_address_mean": transacted_w_address_mean,
        "transacted_w_address_median": transacted_w_address_median,
    }

    return feature_dict


def compute_feature_vector(
    address: str,
    chain: str,
    txs: Optional[List[RawTx]] = None,
) -> np.ndarray:
    """
    Compute 1D/2D feature array with strict schema validation.
    Returns 2D array of shape (1, 55).
    """
    raw_txs = txs or []
    feature_dict = extract_features_from_transactions(address, chain, raw_txs)

    # Assert schema match
    computed_keys = list(feature_dict.keys())
    assert_feature_schema(computed_keys)

    values = [
        float(np.clip(np.nan_to_num(feature_dict[k], nan=0.0, posinf=1000000.0, neginf=0.0), -1e7, 1e7))
        for k in FEATURE_COLUMNS
    ]
    return np.array(values, dtype=np.float32).reshape(1, -1)

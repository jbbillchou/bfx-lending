"""
市場指標（已停用）。
保留模組以維持 import 相容性，所有函式回傳中性預設值。
"""

import copy


def calculate_market_score(frr_history=None, ask_history=None, settings=None, **kwargs):
    return 1.0, {}


def get_market_state(ratio=1.0, bull_multiplier=1.2, **kwargs):
    return 'neutral'


def apply_market_adjustments(settings, market_state):
    return settings

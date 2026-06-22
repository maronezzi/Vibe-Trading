"""
Feature Engineering para predição de direção de candle.

Gera indicadores técnicos a partir de dados OHLCV.
Módulo 100% isolado.
"""

import numpy as np
import pandas as pd


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adiciona indicadores técnicos ao DataFrame OHLCV.
    
    Features geradas:
    - RSI (14)
    - MACD + Signal + Histogram
    - Bollinger Bands (posição relativa)
    - ATR (14)
    - ADX (14)
    - Volume ratio
    - Price change % (1, 3, 5 candles)
    - High-Low range %
    - VWAP distance
    - Candle body ratio
    - EMA 9/21
    """
    df = df.copy()
    
    # === RSI (14) ===
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    df["rsi_14"] = 100 - (100 / (1 + rs))
    
    # === MACD ===
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    
    # === Bollinger Bands (20, 2) ===
    sma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    df["bb_upper"] = sma20 + 2 * std20
    df["bb_lower"] = sma20 - 2 * std20
    df["bb_position"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"] + 1e-10)
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / (sma20 + 1e-10)
    
    # === ATR (14) ===
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean()
    df["atr_pct"] = df["atr_14"] / (df["close"] + 1e-10) * 100
    
    # === ADX (14) ===
    plus_dm = df["high"].diff()
    minus_dm = -df["low"].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    atr_smooth = tr.rolling(14).mean()
    plus_di = 100 * (plus_dm.rolling(14).mean() / (atr_smooth + 1e-10))
    minus_di = 100 * (minus_dm.rolling(14).mean() / (atr_smooth + 1e-10))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10))
    df["adx_14"] = dx.rolling(14).mean()
    
    # === Volume ===
    df["volume_sma20"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / (df["volume_sma20"] + 1e-10)
    
    # === Price Change % ===
    for lag in [1, 3, 5]:
        df[f"close_pct_{lag}"] = df["close"].pct_change(lag) * 100
    
    # === High-Low Range ===
    df["high_low_range"] = ((df["high"] - df["low"]) / (df["close"] + 1e-10)) * 100
    
    # === VWAP (rolling 20) ===
    vwap = (df["close"] * df["volume"]).rolling(20).sum() / (df["volume"].rolling(20).sum() + 1e-10)
    df["vwap_distance"] = ((df["close"] - vwap) / (vwap + 1e-10)) * 100
    
    # === Candle Body ===
    df["body_ratio"] = ((df["close"] - df["open"]).abs() / (df["high"] - df["low"] + 1e-10))
    df["is_bullish"] = (df["close"] > df["open"]).astype(int)
    
    # === EMAs ===
    df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema_cross"] = ((df["ema_9"] - df["ema_21"]) / (df["close"] + 1e-10)) * 100
    
    return df


def add_target(df: pd.DataFrame, horizon: int = 1) -> pd.DataFrame:
    """
    Adiciona target: direção do próximo candle.
    
    target = 1 se close[t+horizon] > close[t] (alta)
    target = 0 se close[t+horizon] <= close[t] (baixa/flat)
    """
    df = df.copy()
    df["target"] = (df["close"].shift(-horizon) > df["close"]).astype(int)
    return df


def get_feature_columns() -> list[str]:
    """Retorna lista das features para o modelo."""
    return [
        "rsi_14", "macd", "macd_signal", "macd_hist",
        "bb_position", "bb_width",
        "atr_pct", "adx_14",
        "volume_ratio",
        "close_pct_1", "close_pct_3", "close_pct_5",
        "high_low_range",
        "vwap_distance",
        "body_ratio", "is_bullish",
        "ema_cross",
    ]


def prepare_dataset(df: pd.DataFrame, horizon: int = 1) -> pd.DataFrame:
    """
    Pipeline completo: indicadores + target + limpeza.
    Remove NaNs causados por rolling windows.
    """
    df = add_technical_indicators(df)
    df = add_target(df, horizon=horizon)
    
    feature_cols = get_feature_columns()
    df = df.dropna(subset=feature_cols + ["target"]).reset_index(drop=True)
    
    return df


if __name__ == "__main__":
    # Teste rápido com dados sintéticos
    np.random.seed(42)
    n = 500
    dates = pd.date_range("2024-01-01", periods=n, freq="5min")
    price = 5000 + np.cumsum(np.random.randn(n) * 5)
    
    test_df = pd.DataFrame({
        "datetime": dates,
        "open": price + np.random.randn(n) * 2,
        "high": price + abs(np.random.randn(n) * 5),
        "low": price - abs(np.random.randn(n) * 5),
        "close": price,
        "volume": np.random.randint(100, 10000, n),
    })
    
    result = prepare_dataset(test_df)
    print(f"Features: {len(get_feature_columns())}")
    print(f"Linhas: {len(result)}")
    print(f"Distribuição target: {result['target'].value_counts().to_dict()}")
    print(result[get_feature_columns() + ["target"]].tail())

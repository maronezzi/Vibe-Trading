"""
LightGBM Direction Classifier — MVP de predição de direção.

Prevê se o próximo candle será de ALTA (1) ou BAIXA/FLAT (0).
Módulo 100% isolado do projeto principal.
"""

import json
import os
import pickle
from datetime import datetime
from typing import Optional

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import TimeSeriesSplit

from prediction.utils.features import get_feature_columns, prepare_dataset

# Path para salvar modelos
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "saved_models")
os.makedirs(MODELS_DIR, exist_ok=True)


class DirectionClassifier:
    """
    Classificador de direção de candle usando LightGBM.
    
    Uso:
        clf = DirectionClassifier(symbol="WDO$", timeframe="M5")
        clf.train(df)
        signal = clf.predict(current_features)
        # signal = {"direction": "UP", "confidence": 0.72, "prob_up": 0.72}
    """
    
    def __init__(
        self,
        symbol: str = "WDO$",
        timeframe: str = "M5",
        horizon: int = 1,
        min_confidence: float = 0.55,
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.horizon = horizon
        self.min_confidence = min_confidence
        self.model: Optional[lgb.LGBMClassifier] = None
        self.feature_cols = get_feature_columns()
        self.train_metrics: dict = {}
    
    def train(self, df: pd.DataFrame, verbose: bool = True) -> dict:
        """
        Treina o classificador com TimeSeriesSplit.
        
        Args:
            df: DataFrame OHLCV (raw, sem features)
            verbose: se imprime métricas
            
        Returns:
            dict com métricas de treino
        """
        # Preparar dataset
        dataset = prepare_dataset(df, horizon=self.horizon)
        
        X = dataset[self.feature_cols]
        y = dataset["target"]
        
        if verbose:
            print(f"\n{'='*60}")
            print(f"TREINO: {self.symbol} {self.timeframe}")
            print(f"{'='*60}")
            print(f"Amostras: {len(X)} | Features: {len(self.feature_cols)}")
            print(f"Distribuição: UP={y.sum()} ({y.mean()*100:.1f}%) | DOWN={len(y)-y.sum()} ({(1-y.mean())*100:.1f}%)")
        
        # Time Series Split (5 folds)
        tscv = TimeSeriesSplit(n_splits=5)
        fold_metrics = []
        
        for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
            X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]
            
            # LightGBM
            model = lgb.LGBMClassifier(
                n_estimators=500,
                learning_rate=0.05,
                max_depth=6,
                num_leaves=31,
                min_child_samples=20,
                subsample=0.8,
                colsample_bytree=0.8,
                reg_alpha=0.1,
                reg_lambda=0.1,
                random_state=42,
                verbose=-1,
            )
            
            model.fit(
                X_train, y_train,
                eval_set=[(X_val, y_val)],
                callbacks=[lgb.log_evaluation(0)],  # silencioso
            )
            
            # Métricas
            y_pred = model.predict(X_val)
            y_prob = model.predict_proba(X_val)[:, 1]
            
            acc = accuracy_score(y_val, y_pred)
            f1 = f1_score(y_val, y_pred)
            prec = precision_score(y_val, y_pred, zero_division=0)
            rec = recall_score(y_val, y_pred, zero_division=0)
            
            fold_metrics.append({
                "fold": fold + 1,
                "accuracy": acc,
                "f1": f1,
                "precision": prec,
                "recall": rec,
                "n_train": len(train_idx),
                "n_val": len(val_idx),
            })
            
            if verbose:
                print(f"  Fold {fold+1}: acc={acc:.4f} f1={f1:.4f} prec={prec:.4f} rec={rec:.4f} (n={len(val_idx)})")
        
        # Treinar modelo final com TODOS os dados
        self.model = lgb.LGBMClassifier(
            n_estimators=500,
            learning_rate=0.05,
            max_depth=6,
            num_leaves=31,
            min_child_samples=20,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=0.1,
            random_state=42,
            verbose=-1,
        )
        self.model.fit(X, y)
        
        # Métricas finais
        y_pred_all = self.model.predict(X)
        self.train_metrics = {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "horizon": self.horizon,
            "n_samples": len(X),
            "n_features": len(self.feature_cols),
            "target_distribution": {"UP": int(y.sum()), "DOWN": int(len(y) - y.sum())},
            "fold_metrics": fold_metrics,
            "mean_accuracy": np.mean([f["accuracy"] for f in fold_metrics]),
            "mean_f1": np.mean([f["f1"] for f in fold_metrics]),
            "mean_precision": np.mean([f["precision"] for f in fold_metrics]),
            "mean_recall": np.mean([f["recall"] for f in fold_metrics]),
            "train_date": datetime.now().isoformat(),
        }
        
        if verbose:
            print(f"\n--- MÉDIA DOS FOLDS ---")
            print(f"Accuracy:  {self.train_metrics['mean_accuracy']:.4f}")
            print(f"F1-Score:  {self.train_metrics['mean_f1']:.4f}")
            print(f"Precision: {self.train_metrics['mean_precision']:.4f}")
            print(f"Recall:    {self.train_metrics['mean_recall']:.4f}")
            
            # Feature importance
            imp = pd.Series(
                self.model.feature_importances_,
                index=self.feature_cols
            ).sort_values(ascending=False)
            print(f"\n--- TOP 10 FEATURES ---")
            for feat, val in imp.head(10).items():
                print(f"  {feat:20s} {val:6d}")
        
        return self.train_metrics
    
    def predict(self, features: pd.DataFrame) -> dict:
        """
        Prediz direção do próximo candle.
        
        Args:
            features: DataFrame com UMA linha e as features necessárias
            
        Returns:
            dict: {"direction": "UP"/"DOWN", "confidence": float, "prob_up": float}
        """
        if self.model is None:
            raise RuntimeError("Modelo não treinado. Chame train() primeiro.")
        
        X = features[self.feature_cols].iloc[[-1]].astype(float)
        prob_up = self.model.predict_proba(X)[0][1]
        direction = "UP" if prob_up > 0.5 else "DOWN"
        confidence = prob_up if prob_up > 0.5 else 1 - prob_up
        
        return {
            "direction": direction,
            "confidence": round(confidence, 4),
            "prob_up": round(prob_up, 4),
            "actionable": confidence >= self.min_confidence,
        }
    
    def save(self, tag: str = "latest") -> str:
        """Salva modelo e metadata."""
        model_path = os.path.join(MODELS_DIR, f"{self.symbol}_{self.timeframe}_{tag}.pkl")
        meta_path = os.path.join(MODELS_DIR, f"{self.symbol}_{self.timeframe}_{tag}_meta.json")
        
        with open(model_path, "wb") as f:
            pickle.dump(self.model, f)
        
        meta = {
            **self.train_metrics,
            "feature_cols": self.feature_cols,
            "model_path": model_path,
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2, default=str)
        
        print(f"[OK] Modelo salvo: {model_path}")
        return model_path
    
    def load(self, tag: str = "latest") -> bool:
        """Carrega modelo salvo."""
        model_path = os.path.join(MODELS_DIR, f"{self.symbol}_{self.timeframe}_{tag}.pkl")
        if not os.path.exists(model_path):
            print(f"[ERRO] Modelo não encontrado: {model_path}")
            return False
        
        with open(model_path, "rb") as f:
            self.model = pickle.load(f)
        
        print(f"[OK] Modelo carregado: {model_path}")
        return True


if __name__ == "__main__":
    # Teste com dados sintéticos
    from prediction.data.mt5_fetcher import fetch_ohlcv
    
    print("=== Teste DirectionClassifier ===\n")
    
    # Buscar dados reais
    df = fetch_ohlcv("WDO$", "M5", 2000)
    if df is not None:
        clf = DirectionClassifier(symbol="WDO$", timeframe="M5")
        metrics = clf.train(df)
        clf.save("test")
        
        # Testar predição no último candle
        from prediction.utils.features import add_technical_indicators
        ds = prepare_dataset(df)
        last_row = ds.iloc[[-1]]
        signal = clf.predict(last_row)
        print(f"\nSinal atual: {signal}")
    else:
        print("MT5 não disponível — teste com dados sintéticos")
        np.random.seed(42)
        n = 1000
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
        
        clf = DirectionClassifier()
        clf.train(test_df)

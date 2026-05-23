"""shiyi-common 工具函数"""

from typing import List, Dict, Any


def energy_to_layer(energy: float) -> str:
    """根据能量值判断记忆层级
    
    Args:
        energy: 能量值 (0.0 ~ 1.0)
        
    Returns:
        "hot" (>=0.7), "warm" (>=0.3), "cold" (<0.3)
    """
    if energy >= 0.7:
        return "hot"
    elif energy >= 0.3:
        return "warm"
    else:
        return "cold"


def truncate_text(text: str, max_len: int = 200) -> str:
    """截断文本"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."

"""Fuzi 标准基准测试集

12 条测试覆盖 7 种意图类型：
- fact: 实体事实查询、跨轮次信息、用户偏好
- emotion: 情绪回顾、情感筛选
- entity: 综合画像、最近动态
- recall: 全量回溯、模糊回忆
- knowledge: 技术主题检索
- time: 时间锚定回忆
- mixed: 事实+情感综合
"""

STANDARD_BENCHMARKS = [
    # ═══ fact（实体事实查询）═══
    {
        "id": "fact_entity",
        "query": "张三是什么职业？",
        "intent_hint": "fact",
        "expected": ["张三", "程序员"],
        "layer_weights": {"fts5": 0.8, "vector": 0.2},
        "description": "精确事实：实体姓名 + 属性查询",
    },
    {
        "id": "fact_cross",
        "query": "我记得之前说过住在哪里",
        "intent_hint": "fact",
        "expected": ["北京", "朝阳"],
        "layer_weights": {"vector": 0.7, "fts5": 0.3},
        "description": "跨轮次事实：隐含查询，需语义检索",
    },
    {
        "id": "fact_preference",
        "query": "我喜欢什么运动？",
        "intent_hint": "fact",
        "expected": ["骑", "自行车"],
        "layer_weights": {"fts5": 0.5, "vector": 0.5},
        "description": "用户偏好：个人喜好检索",
    },

    # ═══ emotion（情感检索）═══
    {
        "id": "emotion_review",
        "query": "最近遇到什么开心的事？",
        "intent_hint": "emotion",
        "expected": ["升职", "项目", "过"],
        "layer_weights": {"vector": 0.6, "fts5": 0.4},
        "description": "情绪回顾：正向事件查询",
    },
    {
        "id": "emotion_filter",
        "query": "最近有什么让我难过的事？",
        "intent_hint": "emotion",
        "expected": ["遗憾", "失败"],
        "layer_weights": {"vector": 0.8, "fts5": 0.2},
        "description": "情感筛选：负向体验查询",
    },

    # ═══ entity（实体聚合）═══
    {
        "id": "entity_profile",
        "query": "说说关于张三的所有记忆",
        "intent_hint": "entity",
        "expected": ["张三", "30岁", "北京"],
        "layer_weights": {"fts5": 0.9, "vector": 0.1, "trigger": 0.5},
        "description": "综合画像：全量实体信息聚合",
    },
    {
        "id": "entity_recent",
        "query": "李四最近在做什么？",
        "intent_hint": "entity",
        "expected": ["李四"],
        "layer_weights": {"fts5": 0.6, "vector": 0.4},
        "description": "最近动态：时间加权的实体查询",
    },

    # ═══ recall（回溯查询）═══
    {
        "id": "recall_full",
        "query": "你还记得我告诉过你什么吗？",
        "intent_hint": "recall",
        "expected": ["张三", "北京", "程序员"],
        "layer_weights": {"trigger": 0.8, "fts5": 0.2, "vector": 0.3},
        "description": "全量回溯：不加限定的回忆请求",
    },
    {
        "id": "recall_fuzzy",
        "query": "之前好像聊过一个人的事，叫什么来着？",
        "intent_hint": "recall",
        "expected": ["张三"],
        "layer_weights": {"vector": 0.7, "trigger": 0.3},
        "description": "模糊回忆：模糊关键词的全量搜索",
    },

    # ═══ knowledge（知识检索）═══
    {
        "id": "knowledge_tech",
        "query": "Python 虚拟环境怎么配置？",
        "intent_hint": "knowledge",
        "expected": ["venv", "Python"],
        "layer_weights": {"fts5": 0.9, "vector": 0.1},
        "description": "技术主题：精确关键词匹配",
    },

    # ═══ time（时间查询）═══
    {
        "id": "time_anchor",
        "query": "上个月发生了什么重要的事？",
        "intent_hint": "time",
        "expected": ["离职", "项目"],
        "layer_weights": {"fts5": 0.4, "vector": 0.4, "trigger": 0.2},
        "description": "时间锚定：时间范围检索",
    },

    # ═══ mixed（混合意图）═══
    {
        "id": "mixed_entity_emotion",
        "query": "我和张三最近关系怎么样？",
        "intent_hint": "mixed",
        "expected": ["张三", "吵架", "开心"],
        "layer_weights": {"fts5": 0.5, "vector": 0.4, "trigger": 0.2},
        "description": "混合意图：实体 + 情感 + 时间综合",
    },
]

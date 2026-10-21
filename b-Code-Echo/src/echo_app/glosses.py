"""高频词的短释义。

ECDICT 的义项顺序不按常用度排，高频词最容易排错：good 排成「善行」、like 排成「相似的」、
a 排成「第一个字母 A」。这里只收最常见、脱离上下文也基本不会错的词，其余仍查词典。
"""

GLOSSES = {
    # 代词
    "i": "我", "me": "我", "my": "我的", "mine": "我的", "myself": "我自己",
    "you": "你", "your": "你的", "yours": "你的", "yourself": "你自己",
    "he": "他", "him": "他", "his": "他的", "she": "她", "her": "她",
    "it": "它", "its": "它的", "we": "我们", "us": "我们", "our": "我们的",
    "they": "他们", "them": "他们", "their": "他们的",
    "this": "这", "that": "那", "these": "这些", "those": "那些",
    "what": "什么", "which": "哪个", "who": "谁", "whom": "谁", "whose": "谁的",
    "something": "某事", "anything": "任何事", "everything": "一切", "nothing": "没什么",
    "someone": "某人", "anyone": "任何人", "everyone": "每个人",
    # be 与助动词、情态动词
    "be": "是", "am": "是", "is": "是", "are": "是", "was": "是", "were": "是",
    "been": "是", "being": "是", "do": "做", "does": "做", "did": "做",
    "have": "有", "has": "有", "had": "有",
    "can": "能", "could": "能", "will": "将", "would": "会", "shall": "将",
    "should": "应该", "may": "可以", "might": "可能", "must": "必须",
    # 冠词、连词、介词
    "a": "一个", "an": "一个", "the": "这个", "and": "和", "or": "或", "but": "但是",
    "so": "所以", "because": "因为", "if": "如果", "than": "比", "as": "作为",
    "of": "的", "to": "到", "in": "在…里", "on": "在…上", "at": "在", "for": "为了",
    "with": "和", "about": "关于", "from": "从", "by": "被", "into": "进入",
    "like": "喜欢", "up": "向上", "out": "出", "over": "超过", "after": "之后",
    "before": "之前", "then": "然后", "when": "什么时候", "where": "哪里", "why": "为什么",
    "how": "怎么", "there": "那里", "here": "这里",
    # 高频副词、形容词
    "not": "不", "no": "不", "yes": "是的", "very": "非常", "too": "也",
    "also": "也", "just": "只是", "really": "真的", "well": "好", "good": "好的",
    "great": "很棒的", "nice": "好的", "sure": "当然", "okay": "好的", "ok": "好的",
    "all": "所有", "some": "一些", "any": "任何", "many": "许多", "much": "很多",
    "more": "更多", "most": "最", "other": "其他", "another": "另一个",
    "new": "新的", "old": "旧的", "own": "自己的", "same": "相同的",
    "favorite": "最喜欢的", "favourite": "最喜欢的", "awesome": "太棒了",
    # 高频实义动词
    "get": "得到", "go": "去", "make": "做", "know": "知道", "think": "认为",
    "take": "拿", "see": "看见", "come": "来", "want": "想要", "look": "看",
    "use": "用", "find": "找到", "give": "给", "tell": "告诉", "try": "尝试",
    "feel": "感觉", "need": "需要", "let": "让", "say": "说", "talk": "谈话",
    "thank": "感谢", "thanks": "谢谢", "hi": "你好", "hello": "你好",
}

# 缩写的后半截：that's → 那 + 是
CONTRACTION_TAILS = {"'s": "是", "'m": "是", "'re": "是", "'ve": "已", "'ll": "将", "'d": "会"}
# n't 前半截要还原：can't → can，won't → will
NOT_BASES = {"ca": "can", "wo": "will", "sha": "shall"}

# Parsel 选择器的"字段缺失 vs 有效空值"契约分析

场景：同一条记录跨页面增量更新。字段未出现 → 保留旧值；显式空值 → 清空旧值；
数值 `0` 与布尔 `false` → 仍是有效采集结果，必须覆盖旧值。

本文基于仓库实际代码（`parsel/selector.py`、`parsel/utils.py`）与可运行样例
（`selector-missing-value-demo/`，用 `.venv/bin/python -I selector-missing-value-demo/verify_missing_vs_empty.py`
可复现全部输出）给出结论。

---

## 1. 从节点匹配到值提取：空值在 Parsel 内部的完整链路

### 1.1 节点匹配层：`xpath()` / `css()` 只回答"有没有节点"

`Selector._xpath()`（`parsel/selector.py:760-810`）把 lxml 的求值结果统一包装：

- 节点集 → 每个节点包成一个 `Selector`，组成 `SelectorList`；
- **XPath 标量**（`string()`、`count()`、`boolean()`、`number()` 的结果）：
  `selector.py:798-799` `if not isinstance(result, list): result = [result]`，
  标量被包成单元素列表，标量本身（`str`/`float`/`bool`）成为 `Selector.root`；
- 未匹配到任何东西 → **空 `SelectorList`**，不抛异常、不留痕迹。

`css()`（`selector.py:812-825`）只是 CSS→XPath 翻译后走同一条路径，语义完全一致。

**关键事实：在这一层，"节点不存在"的唯一信号是 `SelectorList` 长度为 0。**
而"节点存在但没有文本"（如 `<span class="desc"></span>`）对 `::text` /
`text()` 查询来说同样返回空列表——因为空元素本来就没有文本子节点。
缺失与显式空在第一次查询时就可能已经被混同（详见 1.3）。

### 1.2 单值提取层：`get()` 的默认值与序列化

- `SelectorList.get(default=None)`（`selector.py:325-332`）：遍历列表，第一个元素
  直接返回其 `.get()`；**列表为空才返回 `default`**。因此
  `default` 参数能区分"无节点"（→ default）与"节点值为空字符串"（→ `""`，
  default 不生效）。实测：

  ```
  v2 .discount::text .get(default='KEEP')   -> 'KEEP'   # 节点缺失，default 生效
  v2 .link::attr(href) .get(default='KEEP') -> ''       # 属性存在但为空，default 不生效
  ```

- `Selector.get()`（`selector.py:886-906`）：
  - `type in ("text", "json")`：**原样返回 `self.root`**，所以 JSON 的 `0`、`False`、
    `""`、`None` 以原始类型到达调用方（这是唯一能把 `0`/`False` 与字符串区分开的出口）；
  - HTML/XML 元素节点：`etree.tostring(...)` 序列化为字符串；
  - HTML/XML 标量 root（XPath 标量、属性之外的伪元素等）：`tostring` 抛
    `AttributeError/TypeError`，落入 `except` 分支——`True → "1"`、`False → "0"`、
    其余 `str(root)`（`selector.py:901-906`）。于是 XPath 的 `boolean(...)` 假值
    变成字符串 `"0"`，`count(...)` 的零变成 `"0.0"`。

- `SelectorList.getall()`（`selector.py:308-313`）：缺失 → `[]`；
  空属性 → `['']`。**`[]` 与 `['']` 是库返回值中保留得最完好的缺失/空区分。**

- `re()` / `re_first()`（`selector.py:831-884` + `utils.py:66-92`）：先 `self.get()`
  再正则。`re_first` 的 `default` 同样只在"无匹配"时生效；但注意 `re()` 作用在
  已序列化的字符串上，JSON 的 `0`/`False` 到这里已经丢失类型。

### 1.3 真值判断层：`__bool__` 是信息丢失最集中处

`Selector.__bool__`（`selector.py:992-998`）定义为 `bool(self.get())`——
**选择器的真假由"内容的真假"决定，而不是"是否匹配到节点"**（docstring 原话：
"the boolean value of a Selector is given by the contents it selects"，
`tests/test_selector.py:336-346` 的 `test_bool` 把空 `href=""` 为假固化为预期行为）。

由此产生三类混同与一个怪癖：

| 输入 | `bool(selector)` | 问题 |
|---|---|---|
| 属性存在但为空 `href=""` | `False` | 显式空被当作"没采到" |
| JSON `0` / `false`（root 原样返回） | `False` | 有效零值/假值被当作"没采到" |
| 文本 `"0"`（HTML `<price>0</price>`） | `True` | 字符串 `"0"` 非空所以为真——HTML 下零值碰巧安全 |
| XPath `boolean(//nope)` 结果为假 | `True`（！） | 假 → `get()` → `"0"` → 非空字符串为真，语义反转 |

实测输出：

```
bool(Selector of href='')                -> False
bool(Selector of .price::text='0')       -> True
xpath boolean(false) .get()              -> '0'
bool(that Selector)                      -> True      # 怪癖：XPath 假变成了真
bool(jmespath('price')[0])  # JSON 0     -> False     # 有效零值被判假
```

### 1.4 缺失属性与 `attrib`

- 属性查询（`::attr(href)` / `/@href`）：属性缺失 → 空列表 → `get()` 为 `None`；
  属性存在但为空（`href=""`）→ 属性节点存在 → `get()` 为 `""`。
  **`None` vs `""` 在 `.get()` 出口是可区分的**，这是 HTML/XML 下唯一一对
  天然可区分的缺失/空信号。
- `SelectorList.attrib`（`selector.py:337-343`）：空列表返回 `{}`，
  无属性元素也返回 `{}`——**缺失与无属性在此被混同**，不能用作存在性判断。

### 1.5 HTML 与 XML 的差异

对缺失/空语义，两者在 `Selector` 层面行为一致（共用 `_xpath`/`get`），差异来自解析器：

- **空元素的文本**：HTML `<span class="desc"></span>` 与 XML `<desc/>` 都没有文本
  子节点，`::text` / `text()` 均返回空列表。也就是说**在两种格式下，
  "元素显式为空"都无法通过文本查询与"元素不存在"区分**，必须退一步查元素节点本身
  （`len(sel.css('.desc')) == 1`）才能确认存在性。实测：

  ```
  v2 .desc::text .get()      -> None     # 空元素，与缺失同形
  v2 .desc (node exists?)    -> 1        # 元素查询才能区分
  xml //desc/text() .get()   -> None     # XML 同样
  xml //desc node count      -> 1
  ```

- HTML 解析器（`html.HTMLParser`，`selector.py:72-77`）有容错恢复，会把残缺标记
  补全成节点，可能"凭空"造出空元素（如裸 `<div>` 未闭合）；XML 解析器
  （`SafeXMLParser`）对格式错误直接抛 `XMLSyntaxError`。增量采集时，HTML 页面
  里"字段存在但为空"可能其实是容错产物，契约层应把"元素存在 + 无文本"统一视为
  显式空，而不去猜测它是页面本意还是解析器补全。
- XPath 标量在两种格式下走同一代码路径，行为相同（`string(//missing)` 与
  `string(//empty)` 都返回 `""`，见 1.2 实测），`string()` 类表达式天然无法
  区分缺失与空，**契约中应禁用 `string()`/`concat()` 作为存在性判断**。

### 1.6 JSON（jmespath/jsonpath）：缺失键与显式 null 被库主动合并

`Selector.jmespath()`（`selector.py:698-703`）：

```python
result = jmespath.search(query, self._json_data(), **kwargs)
if result is None:
    result = []
```

JSON 中"键不存在"和"键存在但值为 `null`"在 jmespath 里都返回 `None`，
而这一行把 `None` 归一化成空列表——**缺失与显式 null 在库出口被不可逆地合并**。
与之对照，`0`、`false`、`""` 不是 `None`，会被包成单元素列表原样送出：

```
jmespath('discount') .getall()   [missing key  -> []]
jmespath('desc') .getall()       [explicit null -> []!]   # 与缺失同形
jmespath('price') .get()         [0      -> int 0]        # 有效零值保留
jmespath('in_stock') .get()      [false  -> False]        # 有效假值保留
jmespath('link') .get()          [''     -> '']
```

调用方要区分 null 与缺失，只能绕开这一层：`jmespath("keys(@)")` 查键集合，
或在构造 `Selector` 前直接检查原始 `dict`（`'desc' in raw`）。

---

## 2. 方案比较：改库的真值语义 vs 保持接口在采集边界适配

### 2.1 现有返回值中，哪些区别已经丢失（库内不可逆）

| 区别 | 丢失位置 | 后果 |
|---|---|---|
| JSON 缺失键 vs 显式 `null` | `selector.py:699-700`，`None → []` | 调用方从 `SelectorList` 无法恢复，必须查原始 dict |
| 元素缺失 vs 元素存在但无文本（`::text`/`text()` 查询） | lxml 节点集本身：空元素无文本子节点 | 必须另发一次元素查询才能区分 |
| XPath `string()` 标量：缺失 vs 空 | XPath 语义本身，两者都返回 `""` | 换查询写法（`count()` + 节点查询）才能区分 |
| XPath `boolean()` 假 → `"0"` → 真 | `selector.py:902-905` + `__bool__` | 布尔标量的真假在 `bool(sel)` 处反转 |
| `SelectorList.attrib`：空列表 vs 无属性元素 | `selector.py:341-343`，都为 `{}` | 不能用于存在性判断 |
| JSON `0`/`False` 经 `re()`/`re_first()` | 先 `get()` 序列化再正则 | 类型信息丢失，但值本身（`"0"`/`"False"`）仍在 |

### 2.2 哪些区别调用方仍然可以保留（无需改库）

- `SelectorList.get(default)`：`default` 只在空列表时生效 → **节点缺失**（→ default）
  与**值存在但为空字符串**（→ `""`）可分；
- `getall()`：`[]` vs `[""]`；
- 元素查询 + 文本查询两步走：`len(sel.css('.desc'))` 判存在，`::text` 取值；
- JSON：`get()` 原样返回 root，`0`/`False`/`""` 以原始类型送出；缺失/null 用
  `keys(@)` 或原始 dict 预检；
- 属性：`@attr` 的 `None` vs `""`。

### 2.3 方案 A：修改选择器真值/返回语义（不推荐）

设想把 `__bool__` 改为"是否匹配到节点"、或让 `jmespath()` 对 null 返回
`[None]` 而非 `[]`、或让 `get()` 用哨兵替代 `None`：

- **旧调用兼容性**：`if sel:`、`sel.get() or fallback`、`extract_first(default=None)`
  是 Scrapy 生态与大量既有爬虫的惯用法，`test_bool` 还把空属性为假固化成了
  测试。任何一处改动都是静默的行为破坏——`if sel.css('.price'):` 在空元素上
  从假变真，下游分支全部走错。
- **多字段批量提取的一致性**：`getall()` 的类型契约是 `list[str]`。若为多值提取
  引入哨兵/`None` 元素，则 `getall()` 与 `get()` 的空值表示分叉（一个 `[None]`、
  一个 `None`），批量与单值不再能共用合并逻辑；而保持现状时两者一致地用
  "空列表/default"表达缺失。
- 改库影响面是全生态的，收益只是省掉调用方一次存在性查询，得不偿失。

### 2.4 方案 B：保持现有接口，在采集边界适配（推荐）

库已经提供了足够的可区分信号（2.2），缺的是一个**统一的调用约定**：
在爬虫/管道层把每个字段的提取结果规范化为三态（缺失/清空/有效值），
再决定合并策略。代价是每个字段多一次元素存在性查询（O(1) 的同树查询，
可忽略），换来零生态破坏、批量与单值逻辑一致。

**结论：采用方案 B。** 库的语义保持"内容驱动"，缺失/空的业务语义属于采集契约，
不属于选择器。

---

## 3. 可实施的采集契约

### 3.1 三态字段结果

```python
MISSING = object()   # 字段未出现 -> 保留旧值
# 返回 "" 或 None    -> 显式空    -> 清空旧值
# 其余（含 0、False）-> 有效值    -> 覆盖旧值
```

规则（HTML/XML）：

1. **存在性用元素查询，不用值查询**：`if not sel.css(node_q): → MISSING`。
   禁止用 `string(...)`、`concat(...)`、`bool(sel)`、`if value:` 判存在。
2. 节点存在后取值：文本类字段 `sel.css(node_q + '::text').get()` 为 `None`
   （空元素无文本节点）时归一化为 `""`——**节点在而文本不在 = 显式清空**；
   属性类字段直接用 `@attr` 的 `None`（缺失）vs `""`（显式空）。
3. **合并只判哨兵，不判真值**：`if value is MISSING: keep`；其余一律覆盖，
   `"0"`、`"false"`、`""` 都按字面值处理。类型转换（`int`/`float`/布尔解析）
   在覆盖之后、入库之前做，且解析失败要报错而不是回退旧值。
4. JSON 数据源：先 `key in raw_dict`（或 `jmespath('keys(@)')`）判缺失；
   键存在时 `null` → 清空，`0`/`false`/`""` → 有效值。不要对 JSON 字段
   使用 `re()`/`re_first()`（类型已丢失）。
5. 批量提取与单值提取共用同一套规则：多值字段以 `getall()` 的 `[]` 为 MISSING、
   `[""]`/含空串列表为显式空，与单值的"空列表/default"语义对齐。

参考实现（`selector-missing-value-demo/verify_missing_vs_empty.py` 第 5 节，
对 HTML v1→v2 全量验证通过）：

```python
def extract_field(sel, node_query, value_query):
    if not sel.css(node_query):
        return MISSING
    value = sel.css(value_query).get()
    return "" if value is None else value  # 节点存在但无文本/属性 -> 显式空

merged = dict(old)
for field, (nq, vq) in FIELDS.items():
    value = extract_field(sel2, nq, vq)
    if value is MISSING:
        continue            # 保留旧值
    merged[field] = value   # "" 清空；"0"/"false" 有效覆盖
```

### 3.2 反例验证：契约覆盖全部冲突情形

样例页面 `product_v2.html` 相对 v1 的更新：`price→0`（有效零）、
`desc→<span class="desc"></span>`（显式清空）、`stock→content="false"`（有效假）、
`link→href=""`（显式清空）、`discount` 整个节点消失（保留旧值 `5`）。

| 策略 | 实测结果 | 错误 |
|---|---|---|
| 反例 1：真值合并 `if value: 覆盖 else 保留` | `desc`、`link` 保留了旧值 | 显式清空被当成漏采，旧值永远清不掉 |
| 反例 2：无条件 `merged[f] = sel.css(q).get()` | `discount → None`，`desc → None` | 缺失字段被误清空；显式空也写成 `None`，与缺失同形 |
| 反例 3：JSON 上 `value or old` | `price=0`、`in_stock=False` 被 `or` 短路保留旧值 | 有效零值/假值被当成漏采 |
| 反例 4：用 `string(//desc)` 判空 | 缺失与空都返回 `""` | 缺失被误判为清空 |
| **本契约** | `{'price': '0', 'discount': '5', 'desc': '', 'stock': 'false', 'link': ''}` | 全部正确：零/假覆盖、空清空、缺失保留 |

反例 1、3 证明"真值判断"同时伤害显式空与有效零/假；反例 2、4 证明
"单查询取值"同时伤害缺失保留与缺失/空区分。契约的"两次查询 + 哨兵合并"
是唯一同时避开两类错误的结构。

### 3.3 库已有行为 vs 建议增加的约束

**库已有行为（可直接依赖，勿重复实现）：**

- 空 `SelectorList` 表示无匹配；`get(default)` 的 default 仅此时生效（`selector.py:325-332`）；
- `getall()` 的 `[]` vs `['']` 区分缺失与空属性（`selector.py:308-313`）；
- JSON 标量 `0`/`False`/`""` 经 `get()` 原样返回（`selector.py:892-893`）；
- 属性缺失 `None` vs 空属性 `""` 在 `.get()` 出口可分。

**建议增加的约束（采集层实现，不改库）：**

1. 每个字段声明"存在性查询 + 取值查询"二元组，禁止单查询取值后直接合并；
2. 合并层只识别 `MISSING` 哨兵，禁止 `or`/`if value:` 真值合并；
3. JSON 字段在构造 `Selector` 前做键存在性预检，绕开 `jmespath()` 的
   `None → []` 归一化（`selector.py:699-700`）；
4. 禁用 `Selector.__bool__`、`string()` XPath、`SelectorList.attrib` 做空值/存在性判断
   （前两者语义反转或混同，后者缺失与无属性同形）；
5. 类型转换（`"0"→0`、`"false"→False`）在合并之后统一进行，转换失败抛错，
   不允许静默回退旧值——否则解析失败会伪装成"字段缺失"。

# Parsel 网页增量采集中“字段缺失”与“有效空值”的契约分析

- 分析对象：`parsel 1.12.1`，仓库 `task/25`，提交 `bb9f234`（2026-11-12）
- 关键实现：`parsel/selector.py`（1007 行）、`parsel/utils.py`、`parsel/csstranslator.py`
- 验证环境：Python 3.14.2；`lxml 6.1.3`、`jmespath 1.1.0`、`w3lib 2.6.0`、`cssselect 1.6.0`、`python-jsonpath 2.2.2`（未安装 `jsonpath-rfc9535`，故 `_jsonpath_backend()` 走 python-jsonpath；两种后端在 null/空数组形状上的结论已分别复核）
- 回归基线：`.venv/bin/python -m pytest -q` → `637 passed, 4 skipped, 6 xfailed`；`tests/test_selector.py` 单独为 `257 passed, 2 skipped`
- 复现方式：所有样例与输出由四个探针脚本产生，均以 `python -I` 运行（不加载脚本目录/当前目录的模块）：
  - `/tmp/parsel-probe/repro/verify_all.py`：本报告 S1–S16 观测表
  - `/tmp/parsel-probe/repro/contract_final.py`：第 3 节参考实现；页面样例从 `pages/page-*.html` 读取，与 §1.6 的 HTML 块一致
  - `/tmp/parsel-probe/repro/bench_probes.py`：§2.2 性能数据
  - `/tmp/parsel-probe/repro/counterexamples.py`：S6/S7/S11 的补充反例
  - 页面样例：`/tmp/parsel-probe/repro/pages/page-{v1,v2,blocked,redesign}.html`

**结论先行**

1. Parsel 在“节点层”完整保留了“有没有匹配到节点”这一信息（`len()`），但在往“值”走的过程中，**至少四种不同的页面事实会被压成同一个 Python 值**：`None` 同时表示“没匹配到”“匹配到 null”“提取失败”；空字符串同时表示“显式清空”“空白填充”“`string()` 作用于不存在节点”；`'0.0'` 既是“计数为零”又是真值。
2. 因此“字段未出现时保留旧值、显式空值表示清空、零/假仍有效”**不能**由 `.get()` / `bool()` / `or 默认值` 直接表达，必须在采集边界做“存在性探测 + 值提取”两阶段，并把结果装进四态（present / empty / missing / unknown）。
3. 修改库的真值语义（方案 A）会破坏 `tests/test_selector.py::test_bool`、`::test_boolean_result`、`::test_extract_first_default` 与文档承诺，且**仍然无法**让 HTML/XML/JSON 三种载体共享同一套真值——因为它们返回的 Python 类型本来就不同。保持接口、边界适配（方案 B）是唯一能同时满足旧调用兼容与多字段一致性的路径。
4. 仅靠“字段级缺失即保留”仍不安全：模板改版、403/验证码页、截断响应都会让**所有字段同时缺失**，此时“保留旧值”会把记录永久冻结在旧版本。契约必须再加页面级门禁（锚点 + 字段法定人数），并显式承认这一取舍。

---

## 1. 从节点匹配到单值/多值提取的完整链路

### 1.1 匹配层：XPath / CSS / JMESPath / JSONPath

```python
# parsel/selector.py:760-810  Selector._xpath
if self.type not in ("html", "xml", "text"):
    raise ValueError(f"Cannot use xpath on a Selector of type {self.type!r}")
...
try:
    xpathev = _get_xpath_evaluator(query, nsp, self._lxml_smart_strings)
    result = xpathev(root, **kwargs)
except etree.XPathError as exc:
    raise ValueError(f"XPath error: {exc} in {query}")

if not isinstance(result, list):
    result = [result]
```

这一层的关键事实：

| 事实 | 代码位置 | 对更新语义的影响 |
|---|---|---|
| 表达式返回**节点集**时，结果是 `list`，元素个数 = 匹配数，**0 个就是空列表** | `selector.py:798-799` | `len()` 是唯一可靠的“存在性”信号 |
| 表达式返回**标量**（`string()`/`count()`/`number()`/`boolean()`/`false()`）时，被包成 **1 元素列表** | 同上 | “标量恒有结果”，标量的值无法与“节点缺失”区分；`string(//missing)` 与 `string(//empty)` 都得到 `''` |
| 标量列表的 `.root` 保留真实类型（`bool`/`float`/`str`），`.get()` 转成字符串 | `selector.py:886-906` | `count()` 得 `'0.0'`，`boolean()` 得 `'0'`，`number()` 得 `'nan'` |
| CSS 只是 XPath 翻译；`::text` → `…/text()`，`::attr(x)` → `…/@x` | `csstranslator.py:137-151` | CSS 与 XPath 共享同一套空值语义，见 `HTMLTranslator().css_to_xpath('a::attr(href)') == 'descendant-or-self::a/@href'` |
| `SelectorList.xpath/css` 对每个元素求值后 `flatten` | `selector.py:216-253`，`utils.py:12-39` | **跨行/跨记录的结果被拍平**，记录边界丢失（见 verify_all.py S11） |
| JSON 走 `jmespath.search`，`None` 结果被替换为 `[]` | `selector.py:698-703` | **JSON null 与 key 缺失在顶层合并**（见 verify_all.py S9） |
| JSONPath 走 `_compile_jsonpath(query)(data)` 后逐个包装，`None` 保留 | `selector.py:705-721`、`669-678` | JSONPath 能把 null 保留为 1 个匹配，与 JMESPath 不对称 |
| 字符串结果被包装成 `type="text"` 的 Selector | `selector.py:669-678` | 后续 `.re()` 可用，`.attrib` 会抛 `AttributeError` |

### 1.2 结果包装层：四种“取值目标”

同一份 HTML 里，四种探头得到的是**不同种类**的东西：

```python
# /tmp/parsel-probe/repro/verify_all.py  S1/S2（页面 page-v2.html）
'//span[@class="price"]'         n=1  get='<span class="price"></span>'   root=HtmlElement
'//span[@class="price"]/text()'  n=0  get=None                          getall=[]
'string(//span[@class="price"])' n=1  get=''                            root=str
'//article/@data-sku'            n=0  get=None                          getall=[]
```

- **元素节点**：`get()` 返回序列化后的 HTML/XML（`etree.tostring`，`selector.py:894-900`），所以“元素存在但为空”得到的是非空字符串 `<span class="price"></span>`——它**既不等于空串，也不等于无匹配**。
- **文本节点**：`text()` 只选直接文本子节点。空元素、只含子元素的元素、只含注释的元素都**没有文本节点**，所以 `n=0`，与“元素不存在”完全一样（`docs/usage.rst:330-331` 也明说 “an empty element has none”）。
- **属性节点**：`@href` 缺失时 `n=0`；属性存在但为空时 `n=1, get=''`。
- **XPath 标量**：永远 `n=1`；`string()` 会把“节点不存在”和“节点为空”都变成 `''`。

### 1.3 取值层：`get` / `getall` / `attrib` / `re` / `bool`

```python
# selector.py:325-332
def get(self, default: str | None = None) -> Any:
    for x in self:
        return x.get()
    return default
```

- `SelectorList.get(default)` **只在列表为空时**返回 default；只要有一个匹配，哪怕它的值是 `''`、`0`、`False`、`None`，也原样返回（`default` 不参与“空值”判断）。
- `Selector.get()` **没有 default 参数**（`selector.py:886`），传参会 `TypeError`。
- `SelectorList.getall()` 的注解是 `list[str]`（`selector.py:308`），但 JSON 结果里实际可能是 `[0]`、`[False]`、`[None]`、`[[]]`——**类型注解与行为不符**，调用方若按字符串处理会踩坑。
- `Selector.attrib`（`selector.py:982-990`）在 `html/xml` 下是 `dict(self.root.attrib)`；在文本/属性节点上会抛 `AttributeError`（`'str' object has no attribute 'attrib'`）；JSON 下返回 `{}`。
- `SelectorList.attrib`（`selector.py:337-343`）对空列表返回 `{}`，对文本/属性节点列表抛 `AttributeError`，对“元素存在但无属性”也返回 `{}`——**“没有元素”与“元素没有属性”无法区分**。
- `Selector.re()`（`selector.py:831-845`）先 `self.get()` 再正则：因此对**元素**做 `re()` 是在序列化后的**标记文本**上搜索。实测 `//span[@data-price="19.99"]` 的 `.re(r'\d+')` 返回 `['19','99']`；对空元素则返回 `[]`。
- `extract_regex`（`utils.py:66-92`）在正则能匹配空串时返回 `''` 而不是 `[]`；`re_first(r'\d*', default=...)` 在空文本上返回 `''`，**不会**落到 default。

### 1.4 真值判断

```python
# selector.py:992-998
def __bool__(self) -> bool:
    """... the boolean value of a Selector is given by the contents it selects."""
    return bool(self.get())
```

`SelectorList` 是 `list` 子类（`selector.py:166`），`bool()` 就是**长度**；`Selector` 的 `bool()` 是**取值的字符串真值**。两者语义不同，且同一事实在不同载体下不同（原始输出见 verify_all.py S5）：

| 页面事实 | `len()` | `bool(SelectorList)` | `bool(Selector)` | `.get()` |
|---|---|---|---|---|
| 元素存在且为空 | 1 | True | True | `'<span …></span>'` |
| 空元素的 `text()` | 0 | False | n/a | `None` |
| 元素不存在 | 0 | False | n/a | `None` |
| `string(//exists-empty)` | 1 | True | **False** | `''` |
| `string(//missing)` | 1 | True | **False** | `''` |
| `count(//missing)` | 1 | True | **True** | `'0.0'` |
| `boolean(//missing)` | 1 | True | **True** | `'0'` |
| JSON `0` | 1 | True | **False** | `0` |
| JSON `false` | 1 | True | **False** | `False` |
| JSON `null`（顶层） | 0 | False | n/a | `None` |
| XML `'0'` 文本 | 1 | True | **True** | `'0'` |

这就是“零值/假值被误当漏采”的根源：**JSON 的 `0`/`False` 是 falsy，XML/HTML 的 `"0"`/`"false"` 是 truthy，而 `count()==0` 反而 truthy**。没有任何一个 `if x:` 写法能同时正确。

### 1.5 默认值与 `or` 惯用法

`tests/test_selector.py:223-228` 固化的是“列表为空才用默认值”：

```python
assert sel.xpath("//div/text()").extract_first(default="missing") == "missing"
```

因此下面这些“看起来能兜住缺失”的写法，实际效果相反（实测见 verify_all.py S16）：

| 写法 | 期望 | 实际 | 后果 |
|---|---|---|---|
| `sel.get() or old` | 缺失时保留 | JSON `0`/`False`/`""` → old | **零值、假值、空串被清成旧值** |
| `sel.get() or old`（HTML） | 缺失时保留 | 缺失 → old（正确）；空元素 → `'<span…>'` | 空元素被当作“有值” |
| `if sel:` 判存在 | 存在为真 | 空元素 True、`string()` 空 False、JSON 0 False | 语义分裂 |
| `if not sel:` 判缺失 | 缺失为真 | 空元素 False、`count()==0` False | **漏判缺失** |
| `float(sel.get()) != 0` | 缺失为假 | `number()` 缺失得 `nan`，`nan != 0` 为 True | 缺失被当作有效非零 |
| `count(...) == 0` | 计数为零 | `.get()` 是 `'0.0'`，`'0.0' == 0` 为 False | 判断永不成立 |
| `int(sel.get())` | 转整数 | `'0.0'` → `ValueError` | 运行期异常 |
| `re_first(r'\d*', default=X)` | 空时用默认 | 空串返回 `''` | 空被当作有效答案 |

### 1.6 本地页面样例与可核验结果

页面样例（`/tmp/parsel-probe/repro/pages/`）：

```html
<!-- page-v1.html -->
<article id="item-1">
  <h1 class="title">Widget</h1>
  <span class="price">19.99</span>
  <span class="stock">5</span>
  <span class="availability">true</span>
  <span class="note">hello</span>
  <a class="image" href="/img/widget.png">img</a>
  <input class="sale" type="checkbox" value="false">
</article>

<!-- page-v2.html：价格显式清空、库存归零、上下架为 false、
     备注与图片整块消失（模拟字段未出现）、促销状态取反 -->
<article id="item-1">
  <h1 class="title">Widget</h1>
  <span class="price"></span>
  <span class="stock">0</span>
  <span class="availability">false</span>
  <a class="image">img</a>
  <input class="sale" type="checkbox" value="true">
</article>

<!-- page-blocked.html：反爬/权限页 -->
<html><body><h1>Access denied</h1><p>Please complete the captcha…</p></body></html>

<!-- page-redesign.html：模板改版，只剩标题与价格 -->
<article id="item-1"><h1 class="title">Widget</h1><span class="price"></span></article>
```

#### S1：page-v2 的节点层与取值层（HTML）

| 探头 | n | `.get()` | `.getall()` | `.root` 类型 |
|---|---|---|---|---|
| `//h1[@class="title"]` | 1 | `<h1 class="title">Widget</h1>` | 同左 | `HtmlElement` |
| `//h1[@class="title"]/text()` | 1 | `Widget` | `['Widget']` | `str` |
| `//span[@class="price"]` | 1 | `<span class="price"></span>` | 同左 | `HtmlElement` |
| `//span[@class="price"]/text()` | **0** | `None` | `[]` | — |
| `string(//span[@class="price"])` | 1 | `''` | `['']` | `str` |
| `normalize-space(//span[@class="price"])` | 1 | `''` | `['']` | `str` |
| `//span[@class="stock"]/text()` | 1 | `'0'` | `['0']` | `str` |
| `//span[@class="availability"]/text()` | 1 | `'false'` | `['false']` | `str` |
| `//span[@class="note"]` | **0** | `None` | `[]` | — |
| `//span[@class="note"]/text()` | **0** | `None` | `[]` | — |
| `string(//span[@class="note"])` | 1 | `''` | `['']` | `str` |
| `//a[@class="image"]/@href` | **0** | `None` | `[]` | — |
| `//article/@data-sku` | **0** | `None` | `[]` | — |

读法：**“价格被清空”与“备注不存在”在文本层都表现为 `None`，在标量层都表现为 `''`。** 只有 `len('//span[@class="price"]') == 1` 与 `len('//span[@class="note"]') == 0` 能区分二者。

#### S2：存在性探针（HTML）

| 探头 | `.get()` | `.root` | 类型 |
|---|---|---|---|
| `count(//span[@class="price"])` | `'1.0'` | `1.0` | `float` |
| `count(//span[@class="note"])` | `'0.0'` | `0.0` | `float` |
| `count(//span[@class="price"]/text())` | `'0.0'` | `0.0` | `float` |
| `count(//a[@class="image"]/@href)` | `'0.0'` | `0.0` | `float` |
| `boolean(//span[@class="price"])` | `'1'` | `True` | `bool` |
| `boolean(//span[@class="note"])` | `'0'` | `False` | `bool` |

#### S3：`get(default)` 只在“列表为空”时生效

```text
missing text  .get('D')  -> 'D'
empty element .get('D')  -> '<span class="price"></span>'   # 默认值不生效
string()      .get('D')  -> ''                              # 默认值不生效
Selector.get('D')        -> TypeError: Selector.get() takes 1 positional argument but 2 were given
```

**含义**：`get(default=SENTINEL)` 无法把“显式清空”和“缺失”分开——只要匹配到节点，它一定返回节点值。

#### S6 / S6b：XML 与 HTML 的形状差异

无命名空间 XML：

| 探头 | n | get | 说明 |
|---|---|---|---|
| `//price` | 1 | `<price/>` | 空元素存在 |
| `//price/text()` | **0** | `None` | 空元素没有文本节点 |
| `//note/text()` | 1 | `' '` | 空白文本节点**存在** |
| `//cdata/text()` | 1 | `''` | CDATA 空串是**存在**的文本节点 |
| `//selfclose/text()` | 0 | `None` | 自闭合无文本 |
| `//item/@sku` | 1 | `''` | 空属性存在 |
| `//item/@missing` | 0 | `None` | 属性不存在 |
| `count(//item)` | 1 | `'1.0'` | |
| `string(//item)` | 1 | `'\n    Widget\n    \n    0\n     \n…'` | **`string()` 会把记录下所有字段拼在一起** |

默认命名空间 XML（`<catalog xmlns="urn:shop">`）：

```text
before remove_namespaces(): //item -> [] | count(//item) -> 0.0
local-name workaround     -> 1
after  remove_namespaces(): //item -> ['<item …>'] | //price/text() -> []
```

**命名空间未处理时，所有字段都会“缺失”**——这是最容易被误判为“整页字段未出现”的情形。`remove_namespaces()` 能恢复匹配，但**不会**把空元素的 `text()` 变出来（`//price/text()` 仍为 `[]`）。

#### S7 / S8：HTML 与 XML 对布尔属性的处理不同

```text
HTML:
  <input class="sale" type="checkbox" checked>    in_attrib=True   value='checked'
  <input class="sale" type="checkbox" checked=""> in_attrib=True   value=''
  <input class="sale" type="checkbox">            in_attrib=False  value=None

XML（无值属性是语法错误，被解析器丢弃）:
  <input class="sale" checked/>        attrib={'class': 'sale'}          # checked 消失
  <input class="sale" checked=""/>     attrib={'class': 'sale', 'checked': ''}
  <input class="sale" checked="checked"/> attrib={'class': 'sale', 'checked': 'checked'}
```

**结论**：HTML 里“布尔属性是否存在”是合法的三态编码（存在/空/不存在）；XML 里“存在但空”与“不存在”无法用无值属性表达，必须写成显式 `value="true|false"`，否则解析器静默丢属性。

#### S9 / S10：JSON / JMESPath / JSONPath 的三方差异

`{"item": {"title":"Widget","price":0,"flag":false,"sku":"","tags":[],"cleared":null}}`，对 `item` 逐个取值：

| key | 真实值 | JMESPath `n` | JMESPath `.get()` | JSONPath `n` | JSONPath `.get()` | `contains(keys(item),k)` |
|---|---|---|---|---|---|---|
| `title` | `'Widget'` | 1 | `'Widget'` | 1 | `'Widget'` | True |
| `price` | `0` | 1 | `0` | 1 | `0` | True |
| `flag` | `False` | 1 | `False` | 1 | `False` | True |
| `sku` | `''` | 1 | `''` | 1 | `''` | True |
| `tags` | `[]` | **0** | `None` | **1** | `[]` | True |
| `cleared` | `None` | **0** | `None` | **1** | `None` | True |
| `absent` | 不存在 | 0 | `None` | 0 | `None` | **False** |

附加实测：

```text
bracket form [item.cleared] -> [None] n=1
bracket form [item.absent]  -> [None] n=1      # 单靠方括号无法区分，必须配 keys()
bare item.cleared .get('D') -> 'D'             # 裸表达式下 null 被当成缺失
bare item.tags    .get('D') -> 'D'             # 裸表达式下 [] 也被当成缺失
bracket [item.tags]         -> [[]] n=1
jsonpath('$.item.cleared') -> [None] n=1
jsonpath('$.item.absent')  -> [] n=0           # JSONPath 能区分
multiselect hash {title: item.title, price: item.price, absent: item.absent}
  -> {'title': 'T', 'price': None, 'absent': None}   # absent 与 null 合并
```

也就是说：

- **JMESPath 顶层**：null、空数组、缺失三者都变成 `n=0`；`contains(keys(...))` 是唯一可靠的 presence 探针（但对象本身缺失/为 null 时 `keys()` 抛 `JMESPathTypeError`）。
- **JSONPath**：null 与空数组是 `n=1`，缺失是 `n=0`；比 JMESPath 更接近“存在性”语义。注意 `$.item.tags` 空数组返回 `[[]]`（1 个空列表），而下标 `[0]` 为 `n=0`。
- **顶层 `null` 字面量**：`Selector(text='null', type='json')` 的 `root is None`，`jsonpath('$.a')` 与 `jmespath('a')` 都给空——与“文档解析失败”无法区分（见 verify_all.py S13）。

#### S11 / S12：多值提取的两种形态

“多值”在 HTML/XML 里是 `getall()` 的列表，在 JSON 里可能是列表元素本身。两者都保留“值的个数”，但**丢失“属于哪一行/哪条记录”**：

```text
S11  one query: ['x', 'y', 'z']
     per row  : [['x', 'y'], [], ['z']]
     per row n: [2, 0, 1]

S12  hrefs: ['/1', ''] | texts: ['one', 'two', 'three']
     zip   : [('/1', 'one'), ('', 'two')]          # 第二组错位
     per element: [('/1','one'), (None,'two'), ('','three'), (None,None)]
```

同理，JSON 里 `items[*].tags` 仍保留嵌套（`[['a','b'],[],['c']]`），但 `SelectorList.jmespath()` 跨元素再调用会 `flatten` 成 `['a','b','c']`。

#### S13：解析失败、反爬页与“字段缺失”完全同形

```text
truncated html   type=html //article=0 //div=0 root=HtmlElement     # 截断的 <div class="item
blocked page     type=html //article=0 //div=0 root=HtmlElement
empty document   type=html //article=0 //div=0 root=HtmlElement
invalid json     type=json root=None; jsonpath('$.a') -> []
XML without declaration auto-detects as: html
  //item on it: ['<item></item>']       # 标签被小写化
  //Item on it: []
  with type='xml', //Item: ['<Item/>']
type='xml' on 'junk<a/>' -> root=<html>, //a=0, 无异常
```

源码依据：

- 解析器固定 `recover=True`（`selector.py:143`），错误被吞掉；
- `Selector.__slots__` 只有 `_expr/_huge_tree/_text/body/namespaces/root/type`，**不保留 parser 与 error_log**（实测 `hasattr(sel, '_parser') is False`）；
- 当 lxml 在恢复模式下返回 `None` 时，`create_root_node` 用空 `<html/>` 兜底（`selector.py:161-162`），于是“整篇 XML 不可解析”表现为“根为 html、所有查询为空”；`XMLSyntaxError` 只在 `text` 非空且 `error_log` 不含编码错误时重抛（`selector.py:145-149`），而 `recover=True` 下本机 lxml 6.1.3 对畸形输入基本不触发它（`tests/test_selector.py:1186-1192` 也承认“只有部分 libxml2 版本”会抛）；
- 实测 `lxml.html.HTMLParser(recover=True)` 对截断属性/截断标签/未闭合标签的 `error_log` 为空——**连自行预解析也检测不到截断**。

因此，“所有字段都没匹配到”至少有四种成因：字段真的被删、页面改版、响应被截断、响应是权限/验证码页。契约必须把“页面级失败”与“字段级缺失”分开。

#### S14：XPath 标量永远是 1 元素列表

```text
false()        n=1 get='0'      root=False    type=bool
true()         n=1 get='1'      root=True     type=bool
count(//zz)    n=1 get='0.0'    root=0.0      type=float
number(//zz)   n=1 get='nan'    root=nan      type=float
number(//v)    n=1 get='0.0'    root=0.0      type=float
string(//zz)   n=1 get=''       root=''       type=str
//zz           n=0 get=None     root=None     type=NoneType
```

**含义**：只有节点集探头的 `n` 能回答“存在性”。`count()`/`number()`/`string()` 恒为 1，它们的“零值”与“缺失”必须靠语义区分（例如 `count()==0` 表示“没有节点”，`number()` 的 `nan` 表示“不是数字”而不是“0”）。

#### S15：空白策略

| 输入 | `string()` | `normalize-space()` | Python `str.strip()` |
|---|---|---|---|
| 三个空格 | `'   '` | `''` | `''` |
| `&nbsp;` | `'\xa0'` | `'\xa0'`（保留） | `''` |
| 全角空格 `U+3000` | `'　'` | `'　'`（保留） | `''` |
| 换页符 `\x0c`（HTML） | `'\x0c'` | `'\x0c'`（保留） | `''` |

XPath 的 `normalize-space()` 只认 `空格/Tab/CR/LF`；Python 的 `strip()` 额外认 `NBSP/U+3000/FF`。**用 `strip()` 判“是否为空”，会把 `&nbsp;` 这种真实内容误判成显式清空**。契约必须固定一种空白集合（建议 HTML5 集合 `空格/Tab/LF/FF/CR`，与 `w3lib.html.HTML5_WHITESPACE` 一致）。

#### S16：禁止的写法逐条求值

```text
json 0 or old          -> 'OLD'      # 0 被当成缺失
json false or old      -> 'OLD'      # False 被当成缺失
json '' or old         -> 'OLD'      # 空串被当成缺失
xml '0' or old         -> '0'        # HTML/XML 侧零值存活
xml empty or old       -> 'OLD'      # 空元素被当成缺失
xml missing or old     -> 'OLD'
count() get: '0.0' | == 0: False | int(): ValueError
number() get: 'nan' | nan != nan: True | bool(nan): True
re_first(r'\d*') on empty text -> ''     # default 不触发
re_first(r'\d+') on empty text -> 'D'
```

**含义**：同一段 `or` 兜底逻辑在 JSON 与 HTML/XML 上给出**相反**的结论（JSON 的零/假被清掉，HTML 的零值被留下），因此控制流必须由 `state` 驱动，而不是由值的真值驱动。

### 1.7 HTML 与 XML 的差异如何影响更新语义

| 维度 | HTML（`html.HTMLParser`） | XML（`SafeXMLParser`） | 更新语义后果 |
|---|---|---|---|
| 标签/属性大小写 | 统一小写 | 保留 | 未显式 `type="xml"` 时 XPath `//Price` 永远 0 → 误判缺失（verify_all.py S13） |
| 布尔属性 | `checked` → `'checked'`，`checked=""` → `''` | 无值属性被丢弃 | HTML 可三态；XML 必须显式值（verify_all.py S7/S8） |
| 空元素文本节点 | 无 `text()` | 无 `text()`，但 CDATA 空串有 | `text()` 无法区分“空元素”与“无元素”（verify_all.py S1/S6） |
| 空白文本节点 | 保留（`' '`） | 保留（`' '`） | 必须 normalize 后判空（verify_all.py S15） |
| 默认命名空间 | 不适用 | 查询全部落空 | 必须先 `remove_namespaces()` 或注册前缀（verify_all.py S6b） |
| 重复属性 | 保留第一个 | `recover=True` 下保留两个属性节点，`@x` 返回两个值，`.attrib` 只留第一个 | 按属性取值与按 `__getitem__` 取值可能不一致 |
| 解析失败 | 恢复为空/部分文档，`error_log` 不可见 | 同为 `recover=True`：畸形 XML 多被恢复为部分树或空 `<html/>`（本机 lxml 6.1.3 未观察到 `XMLSyntaxError` 真正抛出）；仅在编码错误路径重新抛出 | 都要页面级门禁（verify_all.py S13） |
| 实体/编码 | 恢复 | `SafeXMLParser` 不解析外部实体 | 安全，但与“缺失”同形 |

---

## 2. 两种方案比较

### 2.1 方案 A：修改选择器的真值语义

候选改动与影响：

| 候选 | 做法 | 破坏面 | 能否解决问题 |
|---|---|---|---|
| A1 | `Selector.__bool__` 改为 `len`/存在性 | `tests/test_selector.py:336-344::test_bool` 断言 `href=""` 的 Selector 为 falsy；`docs` 承诺 “boolean value … given by the contents it selects”（`selector.py:992-997`） | 否。空元素将变 True、`string()` 空将变 True，但“空元素”与“缺失”仍需调用方二选一探头 |
| A2 | 给 `Selector.get()` 加 default | 新增 API，向后兼容；`tests:223-228` 只测 list 级 | 否。匹配到空值时仍返回空值，default 不触发 |
| A3 | 新增 `Selector.state` / `exists()` / `SelectorList.state()` | 纯新增，兼容 | **部分可以**。`exists()` 只能回答“有没有匹配到节点”；要回答“字段是否出现”，调用方仍须保证探头是元素/属性而非 `text()`/`string()` |
| A4 | 让 `bool()` 对标量用 `.root` 真值 | `count(//x)` 由 True 变 False；`tests/test_selector.py:306-310::test_boolean_result` 仍断言 `get()=='0'`，但用户 `if sel.xpath('count(...)')` 语义翻转 | 否。`nan` 仍 truthy，`0`/`False`/`''` 与 `'0'` 仍不统一 |
| A5 | 让 JMESPath 的 null 保留为匹配（对齐 JSONPath） | `jmespath('cleared')` 从 `n=0` 变 `n=1`、`getall()` 从 `[]` 变 `[None]`；`tests/test_selector_jsonpath.py:68-73` 只固化 JSONPath 侧 | 只修 JSON 一种载体，且改变既有调用方的 `getall()` 长度 |
| A6 | 统一 `getall()` 返回类型为 `str` | JSON 的 `0`/`False`/`None` 将失去类型 | 否，且破坏数字/布尔采集 |

**方案 A 的根本问题**：真值/默认值都是**按载体**定义的（HTML 文本是 `str`、XPath 标量是 `str(root)`、JSON 是原生类型），不存在一个能同时满足三者的 `__bool__`。把语义塞进 `bool()` 只会让 `if sel:` 更不可预测，而不是更可预测；而“字段是否出现”本质上是**调用方的领域问题**（同一个 `<span class="price">` 空标签，可能是“价格清空”，也可能是“模板占位”）。

### 2.2 方案 B：保持接口，在采集边界适配

不改 `selector.py` 任何一行；用“存在性探针 + 值提取”两阶段，把每次提取归一成四态。优点：

- 零破坏：所有旧调用（`.get()` / `.getall()` / `.extract()` / `or` 惯用法）行为不变；`637 passed` 基线不动。
- 跨载体一致：HTML、XML、JSON 都产出同一种 `Outcome(state, value)`，下游合并逻辑只有一份。
- 可在字段级声明“空值的语义”：例如 `innerHTML` 为空算 `EMPTY`，`<input checked>` 缺失算 `PRESENT(False)` 还是 `UNKNOWN`，由 spec 明确。
- 可携带证据：`state` 之外还能记录探针命中数、页面锚点、响应哈希，便于审计“为什么清空”。

代价：

- 每个字段多一次 `len()`/`count()` 探测。`/tmp/parsel-probe/repro/bench_probes.py` 实测（50 行文档、8 个探头）逐字段探针 `369.6 µs` vs 单次标记查询 `88.8 µs`，逐行 3 探头 `466.6 µs`；单行 3 字段 `presence+string()` 为 `10.7 µs`，朴素 `text()` 为 `6.7 µs`（约 +60%）。相对网络与解析成本可接受。
- spec 变长；调用方必须正确处理命名空间、`type=` 显式化、空白策略。

### 2.3 哪些区别已经丢失，哪些还能由调用方保留

| 区别 | 库内是否保留 | 依据 | 调用方能否恢复 |
|---|---|---|---|
| 元素匹配数 0 vs >0 | **保留** | `len()`、`selector.py:798` | 是（首选存在性探针） |
| 元素存在但文本为空 vs 元素不存在 | **只在“元素节点”层保留**；`text()`/`string()` 层丢失 | verify_all.py S1 | 是：用元素探头 `len()>0` + `string(.)` 判空；**不可**用 `text()` 判存在 |
| 属性存在但为空 vs 属性不存在 | **保留**：`len(@attr)` / `'attr' in el.attrib` | verify_all.py S2、S6 | 是 |
| HTML 布尔属性三态（`checked`/`checked=""`/缺失） | **保留** | verify_all.py S8 | 是 |
| XML 无值属性被解析器丢弃 | **已丢失**（解析阶段） | verify_all.py S7 | 否；只能改文档约定（用 `value=`） |
| 空白文本 vs 真空 | **保留**（`' '` 是 1 个文本节点） | verify_all.py S6、S15 | 是，但须自定归一化集合 |
| XML 解析成功/失败 | **已丢失**（错误被吞、根可能回落 `<html/>`） | verify_all.py S13、`selector.py:143,161` | **否**（`HTMLParser` 的 `error_log` 对截断也为空）；只能页面级启发 |
| 响应是反爬页/改版页 | 已丢失（结构层面） | verify_all.py S13 | 只能页面级门禁 |
| JMESPath 顶层 null vs 缺失 vs 空数组 | **已丢失**（三者 `n=0`） | verify_all.py S9 | 是：`contains(keys(obj),key)` + 方括号取值 |
| JSONPath null vs 缺失 | **保留**（`n=1` vs `n=0`） | verify_all.py S9 | 是 |
| JMESPath 空数组 vs 缺失 | 已丢失（均 `n=0`） | verify_all.py S9 | 是：`contains(keys)` |
| JSON 数字/布尔类型 | `.root`/`get()` 保留；`re()`/字符串化后丢失 | verify_all.py S9、S10 | 是（用 `.root`，勿 `str()`） |
| XPath 标量 `count()==0` vs 0 值 | **部分丢失**：`.root` 是 `0.0`，`.get()` 是 `'0.0'`；但对数组长度而言 `count=0` 与“匹配到 0 个元素”一致 | verify_all.py S2、S14 | 是（读 `.root` 并 `float()`），但要区分“计数 0”与“数值 0”需换探头 |
| `number()` 的 NaN 与“非数字” | **已丢失**（两者都 NaN） | verify_all.py S14 | 否；改用文本 + `Decimal` 解析 |
| 跨记录的多值分组 | **丢失**（`flatten`） | verify_all.py S11 | 是：逐记录迭代 + 相对 XPath |
| 并行列表的字段归属 | **丢失** | verify_all.py S12 | 是：逐元素成对取值，或 XPath 谓词绑定 |
| 联合 XPath（`a &#124; b`）中每个值的来源字段 | **丢失**（`_expr` 是整条表达式） | 实测 | 是：一次查询只服务一个字段 |
| 行内绝对 XPath 的误用 | **可检测**：`AbsoluteXPathWarning` | `selector.py:750-758` | 是（把警告视为错误） |
| 智能字符串的来源（属性名/父节点） | 需 `_lxml_smart_strings=True` 子类；默认关闭 | `selector.py:576`、`tests:858` | 是（自定义子类），但非必需 |
| `getall()` 的静态类型 | **注解错误**（`list[str]` 实际含原生 JSON） | `selector.py:308` | 否（只能运行期判断） |

### 2.4 旧调用兼容与多字段批量提取的一致性取舍

- **旧调用兼容**：方案 B 不触碰任何公开方法，因此 `.get() or old`、`extract_first(default=…)`、`SelectorList.attrib` 的既有行为原样保留。方案 A 中 A1/A4/A5 都会改变旧代码结果（尤其是 `or` 惯用法与 `bool()`），需要一次主版本升级 + 迁移指南。
- **批量一致性**：方案 A 无法做到“同一套真值跨 HTML/XML/JSON 一致”（载体类型不同）；方案 B 在边界统一成四态，因此可以做到。代价是失去“一次查询返回所有字段”的原子性——`{title: item.title, absent: item.absent}` 这类批量对象会把“缺失”和“null”合并（verify_all.py S9/S10），所以批量只适用于**已确认全部存在**的字段；存在性必须逐字段探测（`contains(keys(...))`），或改用 JSONPath 的 `len`。
- **性能取舍**：批量对象（1 次查询）比逐字段 presence+value（2N 次查询）快一个量级（§2.2 实测）。建议：字段数少（≤8）时逐字段两阶段；字段数多且模板稳定时，用一次批量取值 + 逐字段 `keys()` 探针做交叉校验。

---

## 3. 可实施的采集契约

### 3.1 四态状态机

每个字段的提取结果只能是以下四者之一：

| 状态 | 含义 | 合并动作 |
|---|---|---|
| `PRESENT(value)` | 页面显式给出有效值（含 `0`、`False`、`"0"`、`"false"`、空列表以外的列表） | **写入** `value` |
| `EMPTY(value)` | 页面显式表达清空（空元素/空属性/JSON `null`/JSON `""`/空容器） | **写入** `value`（通常 `None`/`""`/`[]`） |
| `MISSING` | 字段结构在本页不存在（元素/属性/key 缺失），且**页面本身可信** | **保留旧值** |
| `UNKNOWN` | 无法判定（父容器缺失、解析失败、类型不匹配、值不可解析） | **保留旧值并告警** |

核心不变量：

1. 任何 `0`/`False`/`"0"`/`"false"` 都必须落在 `PRESENT`，**禁止**经过 `bool()`、`or`、`float(...) != 0`。
2. `EMPTY` 只能由“容器/节点存在且其内容为空”产生，不能由 `string(//missing)` 产生。
3. `MISSING` 必须伴随“父级容器存在”的证据；父级也缺失时降级为 `UNKNOWN`。
4. 一页出现 `UNKNOWN` 或大量 `MISSING` 时，不合并，整页重试/告警。

### 3.2 字段规格（spec）

每个字段声明四件事：**容器探针**、**取值探针**、**空值判据**、**值的载体类型**。

```python
spec = {
    "fields": {
        # 文本：元素探针判存在，string(.) 取值，归一化后判空
        "title":  {"kind": "text", "xpath": '//article[@id="item-1"]//h1[@class="title"]'},
        # 数值：元素探针判存在；空 -> EMPTY(None)；不可解析 -> UNKNOWN（绝不 0）
        "price":  {"kind": "number", "xpath": '//article[@id="item-1"]//span[@class="price"]'},
        # 属性：元素探针判存在；属性不在 attrib 中 -> MISSING；存在且 '' -> EMPTY
        "image":  {"kind": "attr", "xpath": '//article[@id="item-1"]//a[@class="image"]', "attr": "href"},
        # HTML 布尔属性：元素在 -> PRESENT(attr in attrib)；元素不在 -> MISSING
        "sale_on": {"kind": "bool_attr", "xpath": '//article[@id="item-1"]//input[@class="sale"]', "attr": "checked"},
        # JSON：keys() 判存在；[obj.key] 取值；null -> EMPTY(None)；"" -> EMPTY("")
        "sku":    {"kind": "json", "obj": "item", "key": "sku"},
        # JSON 列表：容器存在 + [] -> EMPTY([])；非空 -> PRESENT(list)
        "tags":   {"kind": "json_list", "obj": "item", "key": "tags"},
    },
    "guard": '//article[@id="item-1"]',   # 页面级锚点
}
```

关键实现点（全部基于库中已有能力）：

```python
def text_field(doc, xpath):
    nodes = doc.xpath(xpath)
    if not nodes:                                  # 元素不存在
        return Outcome(MISSING)
    value = collapse(nodes[0].xpath("string(.)").get())
    return Outcome(EMPTY, "") if value == "" else Outcome(PRESENT, value)

def attr_field(doc, xpath, name):
    nodes = doc.xpath(xpath)
    if not nodes:                                  # 宿主元素不存在
        return Outcome(MISSING)
    if name not in nodes[0].attrib:                # 元素在、属性不在
        return Outcome(MISSING)
    value = nodes[0].attrib[name].strip(_MARKUP_WS)
    return Outcome(EMPTY, "") if value == "" else Outcome(PRESENT, value)

def number_field(doc, xpath):
    nodes = doc.xpath(xpath)
    if not nodes:
        return Outcome(MISSING)
    raw = collapse(nodes[0].xpath("string(.)").get())
    if raw == "":
        return Outcome(EMPTY, None)                # 显式清空，不是 0
    try:
        float(raw)
    except ValueError:
        return Outcome(UNKNOWN)                    # 不把坏数据写成 0
    return Outcome(PRESENT, raw)                   # 保留原始字符串，避免 5 -> 5.0 抖动

def json_field(doc, obj, key, multi=False):
    try:
        present = doc.jmespath(f"contains(keys({obj}), {quote(key)})").get()
    except ValueError:                             # 对象缺失/null/非对象
        return Outcome(UNKNOWN)
    if not isinstance(present, bool):
        return Outcome(UNKNOWN)
    if not present:
        return Outcome(MISSING)
    value = doc.jmespath(f"[{obj}.{key}]")[0].root    # 方括号保留 null
    if multi:
        return Outcome(EMPTY, []) if value == [] else Outcome(PRESENT, value)
    if value is None:
        return Outcome(EMPTY, None)
    if value == "":
        return Outcome(EMPTY, "")
    return Outcome(PRESENT, value)
```

> `[obj.key]` 方括号形式把值包成单元素数组，从而**避免 JMESPath 对 `null` 与 `[]` 折叠成“无匹配”**（实测 `[item.n] -> [None]`、`[item.arr] -> [[]]`，而 `item.n` / `item.arr` 都是 `n=0`）。空字符串在两种写法下都是 `n=1`，无需方括号。`contains(keys(...))` 负责区分 null 与缺失。key 含 `-`、空格等非标识符字符时写成 `[item."a-b"]`（实测同样 `n=1`）；参考实现用 `_jmes()` 统一加引号。

### 3.3 合并规则与页面级门禁

```python
def apply_page(old, page, spec):
    doc = Selector(text=page, type=spec.get("type"))
    guard = spec.get("guard")
    if guard and len(doc.xpath(guard)) != 1:
        return old, "rejected: page guard"          # 403/验证码/改版到无锚点
    outcomes = {name: extract(doc, rule) for name, rule in spec["fields"].items()}

    # 门禁 2：法定人数。旧记录里已有的字段，本页至少一半要“有结论”，
    # 否则不合并——避免“部分改版”把其余字段批量清空/冻结。
    expected = [n for n in outcomes if n in old]
    resolved = sum(outcomes[n].state in (PRESENT, EMPTY) for n in expected)
    if expected and resolved * 2 < len(expected):
        return old, "held: quorum not met"

    new = dict(old)
    for name, o in outcomes.items():
        if o.state == PRESENT: new[name] = o.value
        elif o.state == EMPTY: new[name] = o.value
        # MISSING / UNKNOWN -> 保留
    return new, "applied"
```

补充约束：

- **记录级锚点**：列表页要用“行锚点”（如 `//article[@id=$id]`）而不是全页 `//article`，否则第 N 行的字段可能来自第 M 行。
- **相对路径**：行内探头一律 `./` 开头；`AbsoluteXPathWarning` 应作为错误处理（实测嵌套选择器用 `//span` 会跨出当前行）。
- **显式类型**：XML 必须 `type="xml"`，并先 `remove_namespaces()` 或注册前缀；否则大小写/命名空间会让所有字段变 `MISSING`。
- **空白策略固定**：统一使用标记空白集合（空格/Tab/LF/FF/CR），不要用 Python `strip()` 判空（否则 `&nbsp;` 被误清空）。
- **一次查询一个字段**：不要用 `|` 联合 XPath 或跨字段 `//text()` 批量取，联合表达式丢失字段归属。
- **审计字段**：记录 `value_state`（present/empty）与 `last_seen_at`、`source_url`；对 `EMPTY` 额外记录“清空依据”（空元素/空属性/null），使“清空”可回溯。
- **单页清空上限**：`EMPTY` 数超过阈值（如 >50% 字段）时转为人工/二次抓取确认，而不是直接写库。

### 3.4 参考实现与实测输出

完整可运行版本见 `/tmp/parsel-probe/repro/contract_final.py`（第 3.2/3.3 节代码即其核心）。对四个本地页面按序更新，`old` 为 v1 采到的值：

```text
-- v1: applied
     title         Outcome('present', 'Widget')
     price         Outcome('present', '19.99')
     stock         Outcome('present', '5')
     availability  Outcome('present', True)
     note          Outcome('present', 'hello')
     image         Outcome('present', '/img/widget.png')
     sale          Outcome('present', 'false')
     sale_on       Outcome('present', False)
-- v2: applied
     price         Outcome('empty', None)        # 显式清空
     stock         Outcome('present', '0')      # 零仍有效
     availability  Outcome('present', False)    # 假仍有效
     note          Outcome('missing', KEEP)     # 字段未出现 -> 保留 hello
     image         Outcome('missing', KEEP)     # 属性未出现 -> 保留旧 URL
     sale          Outcome('present', 'true')
     sale_on       Outcome('present', False)    # 复选框元素在、checked 不在 -> 假，非缺失
-- blocked: rejected
     __page__      Outcome('unknown', KEEP)
-- redesign: held (quorum not met)
     title         Outcome('present', 'Widget')
     price         Outcome('empty', None)
     stock         Outcome('missing', KEEP)
     ...
final: {'title': 'Widget', 'price': None, 'stock': '0', 'availability': False,
        'note': 'hello', 'image': '/img/widget.png', 'sale': 'true', 'sale_on': False}
```

JSON 记录：

```text
title    Outcome('present', 'T')
price    Outcome('present', 0)         # 0 是有效值
flag     Outcome('present', False)     # False 是有效值
sku      Outcome('empty', '')
tags     Outcome('empty', [])
cleared  Outcome('empty', None)        # 显式 null = 清空
absent   Outcome('missing', KEEP)      # key 不存在 = 保留

非对象根：'null' / '[]' / '<html>not json</html>' -> Outcome('unknown', KEEP)

多值容器：容器缺失 -> missing；容器空 -> empty([])；有项 -> present(['a','b'])
```

### 3.5 反例证明：契约覆盖冲突情形

每条反例都在本地实测过（输出见 `/tmp/parsel-probe/repro/verify_all.py`）。

| # | 反例（页面事实） | 朴素写法结果 | 契约结果 | 覆盖要求 |
|---|---|---|---|---|
| C1 | HTML `<span class="price"></span>`（显式清空） | `.get()` 得 `'<span class="price"></span>'`，被当成有值价格 | `empty`，写 `None` | 空值≠漏采 |
| C2 | HTML 无 `<span class="note">`（字段未出现） | `.get() -> None`，若“None 即清空”则误清 | `missing`，保留 `'hello'` | 缺失≠清空 |
| C3 | `<span class="stock">0</span>` | `if get() or old` → 保留旧值（错误） | `present '0'` | 零值有效 |
| C4 | `<span class="availability">false</span>` | `bool(get())` 对 `'false'` 为 True，但 JSON `false` 为 False，跨载体不一致 | `present False` | 假值有效 |
| C5 | JSON `{"price": 0}` | `jmespath('price').get() or old` → old | `present 0` | 零值有效 |
| C6 | JSON `{"flag": false}` | 同上 → old | `present False` | 假值有效 |
| C7 | JSON `{"cleared": null}` | `jmespath('cleared').get() is None` 与“key 缺失”同形 | `contains(keys)` → `empty None` | null≠缺失 |
| C8 | JSON `{"absent": …}` 缺失 | 批量对象 `{absent: item.absent}` 得 `None`，无法区分 | `missing` | 缺失可识别 |
| C9 | JSON `{"tags": []}` | `jmespath('tags')` 得 `n=0`，与缺失同形 | `contains(keys)` → `empty []` | 空容器≠缺失 |
| C10 | HTML `<input checked>`（无值布尔属性） | `.get()` 取 `@checked` 得 `'checked'` → 若转 int 失败 | 元素在 → `present True` | 布尔三态 |
| C11 | HTML `<input>`（未勾选） | 同上得 `None` → 误判缺失 | 元素在 → `present False` | 缺席≠缺失 |
| C12 | XML `<input checked/>` | 属性被解析器丢弃 | 若用 `bool_attr` 规则会得 False——**这是契约无法修复的已丢失信息**，spec 应改用 `value=` | 明确库能力边界 |
| C13 | XML 默认命名空间 | 所有查询 `n=0` → 全字段 missing → 保留旧值（正确但未更新） | 检测到“锚点也 0” → `unknown`/拒绝，并提示 `remove_namespaces()` | 失败≠缺失 |
| C14 | `type` 未指定，XML 无声明 | 被当 HTML 小写化，`//Price` 永远 0 | spec 强制 `type="xml"` | 配置错误可暴露 |
| C15 | 截断响应 `<div class="item` | `//div` 为 0；`error_log` 也为空 | 锚点 0 或法定人数不足 → 拒绝整页 | 解析失败≠字段缺失 |
| C16 | 403/验证码页 | 所有字段 0 | 锚点 0 → 拒绝整页 | 反爬≠清空 |
| C17 | 模板改版只剩 2 个字段 | 其余 6 个 `missing` → 保留旧值（正确）但 2 个字段被更新/清空 | 法定人数 2/8 不足 → 整页 hold | 部分改版不半更新 |
| C18 | `number(//missing)` | `nan`，`nan != 0` 为 True，被当有效数字 | 元素 0 个 → `missing` | NaN≠0 |
| C19 | `count(//missing)` | `.get()=='0.0'`，`== 0` 为 False，`int()` 抛错 | 不用标量判存在，改用 `len()` | 标量≠存在性 |
| C20 | `re_first(r'\d*', default=…)` 对空文本 | 返回 `''`，default 不触发 | 不依赖 default；先判 `state` | 正则空匹配 |
| C21 | 一行多个标签、另一行无标签 | 单次 `getall` 得 `['x','y','z']`，无法知道哪行空 | 逐行 `['x','y'], [], ['z']` | 多值分组 |
| C22 | 两列并行 `getall` 后 `zip` | 第二对错位到第三个元素 | 逐元素成对取值 | 批量对齐 |
| C23 | 联合 XPath `a &#124; b` | 结果无法归属字段 | 一次查询一个字段 | 字段归属 |
| C24 | `&nbsp;` 作为字段内容 | `str.strip()` 得 `''` → 误清空 | 标记空白集合 → `present '\xa0'` | 空白策略 |
| C25 | 同一页 `0` 出现在 HTML（字符串）与 JSON（整数） | 真值判断相反 | 统一四态 | 跨载体一致 |
| C26 | 记录内 `string(.)` 取整个 `<item>` | 得到 `'Widget 0 …'` 拼接串 | 探头限定到字段元素 | 取值范围 |

### 3.6 库中已有行为 vs 建议增加的约束

| 能力/约束 | 状态 | 依据 |
|---|---|---|
| 匹配数存在性（`len()`） | **已有** | `selector.py:798-810` |
| 元素 vs `text()` 的区分 | **已有**（但需调用方懂得选元素探头） | `docs/usage.rst:330-331` |
| 属性存在性（`'a' in el.attrib`） | **已有** | `selector.py:982-990` |
| 属性节点计数（`count(//e/@a)`） | **已有** | verify_all.py S2 |
| HTML 布尔属性三态 | **已有**（解析器行为） | verify_all.py S8 |
| JSON key 存在性（`contains(keys(obj), k)`） | **已有** | verify_all.py S9 |
| JSONPath null vs missing | **已有** | `tests/test_selector_jsonpath.py:68-73` |
| `get(default)` 仅空列表生效 | **已有** | `selector.py:325-332`、`tests:223-228` |
| 警告嵌套绝对 XPath | **已有** | `selector.py:750-758` |
| `remove_namespaces()` | **已有** | `selector.py:924-941` |
| 显式 `type=` 强制 | **已有** | `selector.py:592-593`、`tests:1125-1143` |
| **字段级四态 `Outcome`** | **建议新增（调用方）** | 本报告 §3.1 |
| **空值判据与归一化空白集合** | **建议新增（调用方）** | verify_all.py S15 |
| **数值不可解析 → UNKNOWN（不写 0/NaN）** | **建议新增（调用方）** | §3.2 |
| **布尔属性缺失 → PRESENT(False) vs 元素缺失 → MISSING** | **建议新增（调用方）** | §3.2 |
| **多值容器：容器缺失 / 空容器 / 有项** | **建议新增（调用方）** | §3.2 |
| **页面锚点门禁 + 字段法定人数** | **建议新增（调用方）** | §3.3 |
| **单页清空上限 / EMPTY 审计字段** | **建议新增（调用方）** | §3.3 |
| **行级相对路径 + 逐行分组** | 建议（库有警告，无强制） | §3.3 |
| **解析失败可观测** | **库内缺失，且无法在调用方完全补救** | verify_all.py S13、`selector.py:143,161` |
| **JMESPath null/空数组 presence** | **库内缺失，需 `keys()` 组合** | verify_all.py S9 |
| **`getall()` 类型注解正确性** | 建议修正（`list[Any]` 或泛型） | `selector.py:308` |
| **`Selector.get(default=…)`** | 不建议新增 | A2 分析：不能区分空值 |

---

## 4. 落地检查清单

1. 解析入口固定 `type=`（HTML/XML/JSON 三选一），XML 先 `remove_namespaces()` 或注册前缀。
2. 每个字段声明 `kind`（text/number/attr/bool_attr/json/json_list），并用**元素或属性**做存在性探针；不要用 `text()`/`string()` 判存在。
3. 数值字段：空 → `EMPTY(None)`；非数字 → `UNKNOWN`；保留原文本或 `Decimal`，不要在增量更新中重复 `float()` 强转。
4. 布尔字段：HTML 布尔属性用“元素在 + 属性在”判 `present`；要表达“清空”必须改用显式值属性或 `data-*` 三态。
5. JSON 字段：`contains(keys(obj), key)` 判存在，`[obj.key]` 取值，分别映射 `null`/`""`/`[]` 为 `EMPTY`。
6. 多值字段：先探容器，再取子项；禁止跨记录 `flatten` 后直接写库。
7. 页面门禁：锚点命中数 ≠ 1 或法定人数不足时整页拒绝，并记录原因。
8. 合并：只写 `PRESENT`/`EMPTY`；`MISSING`/`UNKNOWN` 保留旧值并打点。
9. 监控：统计每页 `missing/unknown/empty` 比例，突增即视为模板变更或反爬。
10. 回归：任何契约改动都要满足既有 `637 passed` 基线，并新增针对 C1–C26 的测试用例。

---

## 5. 复现命令

```bash
# 在仓库根目录（本机已建 .venv，含 lxml/jmespath/python-jsonpath/pytest）
.venv/bin/python -I /tmp/parsel-probe/repro/verify_all.py
.venv/bin/python -I /tmp/parsel-probe/repro/contract_final.py
.venv/bin/python -I /tmp/parsel-probe/repro/bench_probes.py
.venv/bin/python -I /tmp/parsel-probe/repro/counterexamples.py
.venv/bin/python -m pytest -q
```

`python -I` 的作用：脚本不把自身目录和当前目录加入 `sys.path`，避免被同名模块污染；四个脚本只依赖已安装的 `parsel` 与标准库。`contract_final.py` 通过 `__file__` 定位同目录 `pages/`，因此可从任意工作目录运行。

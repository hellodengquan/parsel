"""Verifiable probes for Parsel missing-vs-empty behavior.

Run from the repository root with the repo venv:
    .venv/bin/python -I selector-missing-value-demo/verify_missing_vs_empty.py

Page semantics of pages/product_v2.* (an update to the record in product_v1.html):
  price    -> 0            valid zero, must overwrite old 19.99
  desc     -> ""           explicit empty, must CLEAR old description
  stock    -> false        valid false, must overwrite old true
  link href-> ""           explicit empty attribute, must CLEAR old URL
  discount -> (absent)     field not communicated, must KEEP old value 5
"""

import json
import pathlib

from parsel import Selector

PAGES = pathlib.Path(__file__).parent / "pages"


def show(label, value):
    print(f"{label:<64} {value!r}")


print("=== 1. HTML: missing node vs empty node vs zero/false ===")
sel1 = Selector(text=(PAGES / "product_v1.html").read_text())
sel2 = Selector(text=(PAGES / "product_v2.html").read_text())

show("v2 .discount::text .get()        [absent -> None]", sel2.css(".discount::text").get())
show("v2 .discount::text .getall()     [absent -> []]", sel2.css(".discount::text").getall())
show("v2 .desc::text .get()            [empty elem -> None!]", sel2.css(".desc::text").get())
show("v2 .desc::text .getall()         [empty elem -> []!]", sel2.css(".desc::text").getall())
show("v2 .desc (node exists?)          [-> 1 Selector]", len(sel2.css(".desc")))
show("v2 .desc .get()                  [element serialization]", sel2.css(".desc").get())
show("v2 .price::text .get()           [zero -> '0']", sel2.css(".price::text").get())
show("v2 .link::attr(href) .get()      [empty attr -> '']", sel2.css(".link::attr(href)").get())
show("v2 .link::attr(href) .getall()   [empty attr -> ['']]", sel2.css(".link::attr(href)").getall())
show("v2 .link::attr(missing) .get()   [absent attr -> None]", sel2.css(".link::attr(missing)").get())
show("v1 .link::attr(missing) .getall()", sel1.css(".link::attr(missing)").getall())

print()
print("=== 2. HTML: truthiness conflations ===")
empty_attr = sel2.css(".link::attr(href)")[0]
show("bool(Selector of href='')        [empty value -> False]", bool(empty_attr))
show("bool(Selector of .price::text='0')", bool(sel2.css(".price::text")[0]))
bool_xpath = sel2.xpath("boolean(//span[@class='nope'])")[0] if False else sel2.xpath("boolean(//span[@class='nope'])")[0]
show("xpath boolean(false) .get()      [-> '0']", bool_xpath.get())
show("bool(that Selector)              ['0' is truthy!]", bool(bool_xpath))
show("xpath count(//span[@class='nope']) .get()", sel2.xpath("count(//span[@class='nope'])").getall())
show("xpath string(//span[@class='nope']) .get()  [missing -> '']", sel2.xpath("string(//span[@class='nope'])").get())
show("xpath string(//span[@class='desc']) .get()  [empty   -> '']", sel2.xpath("string(//span[@class='desc'])").get())
show("SelectorList.get(default='KEEP') absent", sel2.css(".discount::text").get(default="KEEP"))
show("SelectorList.get(default='KEEP') empty-attr", sel2.css(".link::attr(href)").get(default="KEEP"))
show("SelectorList.attrib on empty list", sel2.css(".nope").attrib)
show("Selector.attrib of <span class=desc>", sel2.css(".desc").attrib)

print()
print("=== 3. XML: same probes on type='xml' ===")
xsel = Selector(text=(PAGES / "product_v2.xml").read_text(), type="xml")
show("xml //discount/text() .get()     [absent -> None]", xsel.xpath("//discount/text()").get())
show("xml //desc/text() .get()         [<desc/> -> None]", xsel.xpath("//desc/text()").get())
show("xml //desc node count            [element exists]", len(xsel.xpath("//desc")))
show("xml //price/text() .get()        [zero]", xsel.xpath("//price/text()").get())
show("xml //in_stock/text() .get()     ['false' string]", xsel.xpath("//in_stock/text()").get())
show("xml //link/@href .get()          [empty attr -> '']", xsel.xpath("//link/@href").get())
show("xml //link/@missing .get()       [absent attr -> None]", xsel.xpath("//link/@missing").get())

print()
print("=== 4. JSON (jmespath): null vs missing key, 0 and false ===")
jdata = (PAGES / "product_v2.json").read_text()
jsel = Selector(text=jdata)
show("jmespath('discount') .getall()   [missing key -> []]", jsel.jmespath("discount").getall())
show("jmespath('desc') .getall()       [explicit null -> []!]", jsel.jmespath("desc").getall())
show("jmespath('price') .get()         [0 -> int 0]", jsel.jmespath("price").get())
show("jmespath('in_stock') .get()      [false -> False]", jsel.jmespath("in_stock").get())
show("jmespath('link') .get()          ['' -> '']", jsel.jmespath("link").get())
show("bool(jmespath('price')[0])       [0 -> False!]", bool(jsel.jmespath("price")[0]))
show("jmespath('keys(@)') distinguishes null vs absent",
     jsel.jmespath("keys(@)").getall())
raw = json.loads(jdata)
show("raw dict: 'desc' in d / d['desc'] is None", ("desc" in raw, raw["desc"] is None))
show("raw dict: 'discount' in d", "discount" in raw)

print()
print("=== 5. Naive merge vs contract merge (HTML v1 -> v2) ===")
old = {"price": "19.99", "discount": "5", "desc": "first version description",
       "stock": "true", "link": "http://example.com/a-1"}

# Naive: truthiness-based merge (what many spiders effectively do)
naive = dict(old)
for field, query in [("price", ".price::text"), ("discount", ".discount::text"),
                     ("desc", ".desc::text"), ("stock", ".stock::attr(content)"),
                     ("link", ".link::attr(href)")]:
    value = sel2.css(query).get()
    if value:  # falsy -> treated as "not scraped", keep old
        naive[field] = value
    # else: keep old
show("naive merge result", naive)
show("naive mistakes",
     {"desc": "kept old, should be CLEARED" if naive["desc"] != "" else "ok",
      "link": "kept old, should be CLEARED" if naive["link"] != "" else "ok",
      "discount": "ok (kept)" if naive["discount"] == "5" else "wrongly cleared",
      "price": "ok" if naive["price"] == "0" else "zero lost"})

# Naive-2: unconditional overwrite with .get()
naive2 = dict(old)
for field, query in [("price", ".price::text"), ("discount", ".discount::text"),
                     ("desc", ".desc::text"), ("stock", ".stock::attr(content)"),
                     ("link", ".link::attr(href)")]:
    naive2[field] = sel2.css(query).get()
show("naive2 (overwrite with .get())", naive2)
show("naive2 mistakes",
     {"discount": "WRONGLY CLEARED to None" if naive2["discount"] is None else "ok",
      "desc": "None instead of '' (clear looks like missing)" if naive2["desc"] is None else "ok"})

# Contract merge: node existence decides MISSING; value '' means CLEAR
MISSING = object()
FIELDS = {  # field -> (node query, value query)
    "price": (".price", ".price::text"),
    "discount": (".discount", ".discount::text"),
    "desc": (".desc", ".desc::text"),
    "stock": (".stock", ".stock::attr(content)"),
    "link": (".link", ".link::attr(href)"),
}


def extract_field(sel, node_query, value_query):
    if not sel.css(node_query):
        return MISSING
    value = sel.css(value_query).get()
    return "" if value is None else value  # node present but no text/attr -> explicit empty


merged = dict(old)
for field, (nq, vq) in FIELDS.items():
    value = extract_field(sel2, nq, vq)
    if value is MISSING:
        continue  # keep old
    merged[field] = value
show("contract merge result", merged)
show("contract merge check",
     {"price==0-kept-as-'0'": merged["price"] == "0",
      "desc cleared to ''": merged["desc"] == "",
      "link cleared to ''": merged["link"] == "",
      "stock == 'false'": merged["stock"] == "false",
      "discount kept old '5'": merged["discount"] == "5"})

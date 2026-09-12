# Receipt template files

One YAML file per retail chain. Filename is ignored (except a leading `_` marks
it as documentation and skips it); the `code:` field is the identifier.

`app/scanner/templates.py` loads every `*.yaml` here at startup (cached), tries
each `identify:` block against the OCR text of an uploaded receipt, and parses
the first match with that file's `fields` / `line_item` rules.

```yaml
code: atb                     # short id, matches Store.code where one exists
store_name: АТБ               # written to Receipt.shop_name on a match
network_type: supermarket     # supermarket | azs | tech | pharmacy | home | other

identify:                     # a spec with NO markers never auto-matches
  contains: ["АТБ-МАРКЕТ"]    # case-insensitive; ALL must be present
  any: ["ТОВ \"АТБ", "ATB"]   # case-insensitive; at least ONE present
  edrpou: ["30487219"]        # digits-only substring match anywhere in the text

fields:                       # each value is a list of regexes tried in order;
  total:    ['(?:СУМА ДО СПЛАТИ|УСЬОГО)\D{0,4}([\d ]+[.,]\d{2})']
  discount: ['ЗНИЖКА\D{0,4}([\d ]+[.,]\d{2})']
  payment:  ['(ГОТІВКА|КАРТКА|Безготівкова)']
  datetime: ['(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})']   # group 1 = date, group 2 = time

line_item:
  # named groups: name, qty, price, sum  (qty/price optional, sum or price required)
  pattern: '^(?P<name>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s*[xхX*]\s*(?P<price>\d+(?:[.,]\d+)?)\s+(?P<sum>\d+(?:[.,]\d+)?)\s*[АБВ]?$'
  # matched on the line(s) right after a product line; subtracts from that item
  discount_pattern: 'Знижка\D{0,4}(?P<discount>\d+(?:[.,]\d+)?)'

date_formats: ["%d.%m.%Y", "%d.%m.%y"]   # optional; sensible defaults otherwise
```

The parser is best-effort: a partial match still produces a `NormalizedReceipt`.
`app/scanner/reconcile.py` (Σ items vs printed total) is what decides whether the
pipeline trusts the result or escalates to the generic / AI stages.

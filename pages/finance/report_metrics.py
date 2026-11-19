"""Read consolidated dollar facts from the issuer's SEC inline-XBRL periodic reports."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
import re
from xml.etree import ElementTree as ET


CIK = {'AAPL': 320193, 'AMZN': 1018724, 'GOOGL': 1652044, 'META': 1326801,
       'MSFT': 789019, 'NVDA': 1045810, 'TSLA': 1318605}
REVENUE_TAGS = ('Revenues', 'SalesRevenueNet', 'RevenueFromContractWithCustomerExcludingAssessedTax')
BASE_TAGS = {'operating': ('OperatingIncomeLoss',),
             'cash': ('NetCashProvidedByUsedInOperatingActivities',),
             'revenue': REVENUE_TAGS}
# The productive assets line includes intangible assets for NVIDIA.
CAPEX_TAGS = {'AMZN': 'PaymentsToAcquireProductiveAssets',
              'NVDA': 'PaymentsToAcquireProductiveAssets'}
NAMES = ('operating', 'cash', 'capex', 'revenue')


def numeric_fact(fact):
    text = ''.join(fact.itertext()).strip().replace(',', '')
    if not re.fullmatch(r'\d+(?:\.\d+)?', text):
        if fact.get('format', '').split(':')[-1] in ('zero-dash', 'zerodash') and text in ('–', '—', '-'):
            return 0
        return None
    value = Decimal(text).scaleb(int(fact.get('scale', 0)) - 6)
    value = -value if fact.get('sign') == '-' else value
    return int(value) if value == value.to_integral_value() else float(value)


def read_report(symbol, url, raw):
    """Index only visible table facts with this issuer, USD and no segment dimensions."""
    root = ET.fromstring(raw)
    units = {
        n.get('id') for n in root.iterfind('.//{*}unit')
        if [m.text for m in n.iterfind('./{*}measure')] == ['iso4217:USD'] and len(n) == 1
    }
    contexts = {}
    for ctx in root.iterfind('.//{*}context'):
        cik = ctx.findtext('.//{*}identifier', '')
        start, end = (ctx.findtext('.//{*}'+k) for k in ('startDate', 'endDate'))
        if (not cik.isdigit() or int(cik) != CIK[symbol] or not start or not end
                or ctx.find('.//{*}segment') is not None or ctx.find('.//{*}scenario') is not None):
            continue
        contexts[ctx.get('id')] = date.fromisoformat(start), date.fromisoformat(end)
    by_tag = {'us-gaap:'+tag: key for key, tags in BASE_TAGS.items() for tag in tags}
    by_tag['us-gaap:'+CAPEX_TAGS.get(symbol, 'PaymentsToAcquirePropertyPlantAndEquipment')] = 'capex'
    facts = defaultdict(list)
    for tr in root.iterfind('.//{*}tr'):
        # Some reports have layout tables nested around the financial statement.
        # Identify the statement row from its own immediate cells only.
        cells = [c for c in tr if c.tag.rsplit('}', 1)[-1] in ('td', 'th')]
        if not cells or tr.find('.//{*}table') is not None:
            continue
        row_label = ''
        for cell in cells:
            if cell.find('.//{*}nonFraction') is not None:
                break
            words = ' '.join(''.join(cell.itertext()).split())
            if words and words not in ('$', '(', ')'):
                row_label = words
                break
        if not row_label:
            continue
        for f in tr.iterfind('.//{*}nonFraction'):
            tag, ctx = f.get('name'), f.get('contextRef')
            if tag not in by_tag or ctx not in contexts or f.get('unitRef') not in units:
                continue
            value = numeric_fact(f)
            if value is None:
                continue
            start, end = contexts[ctx]
            source = dict(url=url, row=row_label, column=f'{start} – {end}', value=value, tag=tag)
            if f.get('id'):
                source['anchor'] = f.get('id')
            facts[(by_tag[tag], start, end)].append(source)
    by_metric = defaultdict(dict)
    for (key, start, end), matches in facts.items():
        distinct = {m['value'] for m in matches}
        # Never choose among conflicting (e.g. disaggregated) revenue values.
        if len(distinct) == 1:
            by_metric[key][start, end] = matches[0]
    return by_metric


def unique(entries):
    return entries[0] if len(entries) == 1 else None


def days(start, end):
    return (end - start).days + 1


def direct(source):
    return dict(value=source['value'], method='direct', sources=[source])


def quarter_metric(facts, previous_facts, end, prior_end, quarter):
    # Quarter may have 12, 13 or 14 weeks, or use calendar months.
    current = unique([s for (a,b), s in facts.items() if b == end and 77 <= days(a,b) <= 105
                      and (prior_end is None or a == prior_end + timedelta(days=1))])
    if current:
        return direct(current)
    if not previous_facts or quarter == 1 or prior_end is None or not 77 <= days(prior_end + timedelta(days=1), end) <= 105:
        return None
    options = []
    for (a,b), s in facts.items():
        # Same opening balance date in the same fiscal year, ending one quarter apart.
        if b == end and abs(days(a,b) - quarter*91) <= 12:
            prev = previous_facts.get((a, prior_end))
            if prev:
                options.append((s,prev))
    pair = unique(options)
    if pair:
        s, prev = pair
        return dict(value=s['value'] - prev['value'], method='difference', sources=[s, prev])
    return None


def build_report_metrics(symbol, records, get_bytes, previous=None):
    """Return period -> metric -> {value, method, sources} in millions of USD.

    previous may be an original SEC filings index (form, end, url) covering the
    quarter before the window. It is used only as the first quarter's comparator.
    """
    ordered = sorted(records, key=lambda r: r['end'])
    result = {r['period']: {} for r in ordered}
    if not ordered:
        return result
    prev_facts, prev_end, prev_period = {}, None, None
    first = ordered[0]
    first_end = date.fromisoformat(first['end'])
    if previous:
        entries = [previous] if isinstance(previous, dict) else previous
        earlier = [r for r in entries if r['form'] in ('10-Q', '10-K', '10-Q/A', '10-K/A')
                   and 77 <= (first_end - date.fromisoformat(r['end'])).days <= 105]
        p = unique(earlier)
        if p and not p['form'].endswith('/A'):
            prev_facts = read_report(symbol, p['url'], get_bytes(p['url']))
            prev_end = date.fromisoformat(p['end'])
            prev_period = int(first['period'][2:6])*4 + int(first['period'][-1]) - 1
    for rec in ordered:
        period, end = rec['period'], date.fromisoformat(rec['end'])
        fiscal_year, q = int(period[2:6]), int(period[-1])
        rank = fiscal_year*4 + q
        if prev_period != rank - 1:
            prev_end, prev_facts = None, {}
        url = rec.get('quarterly')
        if url and not rec.get('candidates'):
            raw = get_bytes(url)
            expected = rec.get('quarterlySha')
            if expected:
                if sha256(raw).hexdigest() != expected:
                    raise ValueError(f'{symbol} {period}: source hash differs')
            facts = read_report(symbol, url, raw)
            for key in NAMES:
                item = quarter_metric(facts.get(key, {}), prev_facts.get(key, {}), end, prev_end, q)
                if item:
                    result[period][key] = item
            prior_revenue = unique([s for (a,b),s in facts.get('revenue',{}).items()
                                    if 357 <= (end-b).days <= 373 and 77 <= days(a,b) <= 105])
            if prior_revenue:
                result[period]['priorRevenue'] = direct(prior_revenue)
        else:
            facts = {}
        prev_end, prev_facts, prev_period = end, facts, rank
    return result

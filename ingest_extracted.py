"""
ingest_extracted.py — write in-chat-extracted rules to the brain (file-bridge).

Counterpart of harvest_pages.py: takes a JSONL of pages with their extracted
rules (produced by local Claude, not the API) and commits them through the
exact same provenance path the cron uses (db.upsert_document + db.upsert_rule),
then advances url_state / last_crawled / ingest_runs so the weekly cron only
sees future deltas.

Input line shape:
    {"source_id","org","url","title","new_hash","rules":[{name,if_condition,
     then_logic,domain_tag,confidence_score[,status]}, ...]}
Pages with rules=[] are committed as legitimately-empty (url_state advanced).
Rules pass the cron's write-time gate (extract._validate_rules: required
fields + MIN_RULE_CONFIDENCE floor + deprecation screen); status
'deprecated'/'retired'/'sunset' lands on the deprecated path like the cron.

    railway run .venv-local/bin/python ingest_extracted.py <extracted.jsonl>
"""
import json
import sys

from sieve_ingest import db, extract

IN = sys.argv[1] if len(sys.argv) > 1 else 'extracted.jsonl'


def main():
    conn = db.connect()
    run_id = db.start_run(conn)
    print(f'ingest run {run_id} (transport=local-claude)', flush=True)

    totals = {'pages': 0, 'new': 0, 'refreshed': 0, 'dropped': 0, 'invalid_rules': 0}
    detail = {}
    touched_sources = set()
    with open(IN, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            page = json.loads(line)
            sid, org, url = page['source_id'], page['org'], page['url']
            src_d = detail.setdefault(sid, {'changed': 0, 'new': 0, 'refreshed': 0,
                                            'dropped': 0, 'statuses': {}})
            totals['pages'] += 1
            src_d['changed'] += 1
            change_id = db.record_change(conn, run_id, sid, url,
                                         page.get('change_type', 'modified'),
                                         page.get('signal', 'content_hash'),
                                         None, page.get('new_hash'))

            # Same write-time gate as the cron (extract._validate_rules):
            # required fields + confidence floor, and the deprecation screen
            # flags known-dead guidance emitted as active.
            raw = [r for r in (page.get('rules') or []) if isinstance(r, dict)]
            non_dict = len(page.get('rules') or []) - len(raw)
            rules, rejected = extract._validate_rules(raw, url)
            totals['invalid_rules'] += rejected + non_dict

            status = 'ok' if rules else 'empty'
            page_new = page_ref = page_drop = 0
            if rules:
                tag_counts = {}
                for r in rules:
                    tag = r.get('domain_tag') or 'general'
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                doc_tag = max(tag_counts, key=tag_counts.get)
                title = (page.get('title') or rules[0]['name'] or url)[:200]
                doc_id = db.upsert_document(conn, source_url=url, source_org=org,
                                            title=title, domain_tag=doc_tag)
                for r in rules:
                    try:
                        outcome = db.upsert_rule(conn, r, doc_id=doc_id,
                                                 source_url=url, source_org=org,
                                                 status=extract._rule_status(r))
                        if outcome == 'new':
                            page_new += 1
                        else:
                            page_ref += 1
                    except Exception as e:
                        page_drop += 1
                        print(f'  WRITE FAIL {url}: {e}', flush=True)
                if page_drop and not page_new and not page_ref:
                    status = 'write_error'
            src_d['statuses'][status] = src_d['statuses'].get(status, 0) + 1
            src_d['new'] += page_new
            src_d['refreshed'] += page_ref
            src_d['dropped'] += page_drop
            totals['new'] += page_new
            totals['refreshed'] += page_ref
            totals['dropped'] += page_drop
            # Outcome first, consume second (same order as the cron) — a row
            # left 'detected' forever would count as a stale failed attempt in
            # count_extract_failures and eat into the gave-up budget.
            db.update_change_outcome(conn, change_id,
                                     {'ok': 'extracted', 'empty': 'empty',
                                      'write_error': 'failed'}[status],
                                     page_new, page_ref)
            # advance the fingerprint ONLY when the page committed cleanly —
            # same retry semantics as the hardened cron
            if status in ('ok', 'empty') and page.get('new_hash'):
                db.save_url_state(conn, url, sid, None, None, page['new_hash'])
                touched_sources.add(sid)
            print(f'  {url} -> +{page_new} new / {page_ref} refreshed '
                  f'/ {page_drop} dropped [{status}]', flush=True)

    for sid in touched_sources:
        db.mark_source_crawled(conn, sid, None)

    status = 'done' if not totals['dropped'] else 'degraded'
    detail['_transport'] = 'local-claude-file-bridge'
    # plain dict — finish_run wraps it in Json() itself
    db.finish_run(conn, run_id, sources_checked=len(touched_sources),
                  sources_changed=len(touched_sources), urls_changed=totals['pages'],
                  objects_written=totals['new'], status=status, detail=detail)
    print(f'\nDONE run {run_id}: {totals}', flush=True)
    conn.close()


if __name__ == '__main__':
    main()

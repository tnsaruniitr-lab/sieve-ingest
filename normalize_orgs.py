"""
normalize_orgs.py — repair source_org hygiene across the brain (idempotent).

Three passes over sieve.rules / principles / anti_patterns / documents:
  1. ALIAS      — canonicalize known variants ('backlinko.com' → 'Backlinko')
                  via sieve.org_aliases (created+seeded here; extend by INSERT).
  2. DERIVE     — junk orgs ('Personal Blog'/'Unknown'/'') that DO have a
                  source_url get their org derived from the URL host, then
                  pass 1 canonicalizes the derived value.
  3. QUARANTINE — junk org AND no URL → 'unattributed-legacy' so retrieval can
                  down-weight honestly instead of pretending provenance exists.

DATA-SAFE: writes a one-time backup table per run-date before mutating; never
touches rule text; re-running converges (all passes are idempotent).
"""
import os
import sys
from datetime import date

import psycopg2

DB_URL = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')

JUNK_ORGS = ('Personal Blog', 'Unknown', '')

# alias → canonical (lowercased alias match). Seeded from the observed host
# inventory of 2026-07-16 + the source-registry canonical names.
ALIASES = {
    'backlinko.com': 'Backlinko',
    'ycombinator.com': 'Y Combinator',
    'yc startup library': 'Y Combinator',
    'developers.google.com': 'Google',
    'firebase.google.com': 'Google',
    'moz.com': 'Moz',
    'ahrefs.com': 'Ahrefs',
    'semrush.com': 'Semrush',
    'searchengineland.com': 'Search Engine Land',
    'searchenginejournal.com': 'Search Engine Journal',
    'seroundtable.com': 'Search Engine Roundtable',
    'schema.org': 'Schema.org',
    'docs.perplexity.ai': 'Perplexity',
    'perplexity.ai': 'Perplexity',
    'blog.hubspot.com': 'HubSpot',
    'hubspot.com': 'HubSpot',
    'buffer.com': 'Buffer',
    'sproutsocial.com': 'Sprout Social',
    'review.firstround.com': 'First Round Review',
    'firstround.com': 'First Round Review',
    'forentrepreneurs.com': 'For Entrepreneurs',
    'a16z.com': 'a16z',
    'appsflyer.com': 'AppsFlyer',
    'reforge.com': 'Reforge',
    'saastr.com': 'SaaStr',
    'gong.io': 'Gong',
    'openviewpartners.com': 'OpenView',
    'openview': 'OpenView',
    'demandcurve.com': 'Demand Curve',
    'animalz.co': 'Animalz',
    'almcorp.com': 'ALM Corp',
    'andrewchen.com': 'Andrew Chen',
    'aprildunford.com': 'April Dunford',
    'lennysnewsletter.com': "Lenny's Newsletter",
    'cxl.com': 'CXL',
    'frase.io': 'Frase',
    'orbitmedia.com': 'Orbit Media',
    'apptweak.com': 'AppTweak',
    'nealschaffer.com': 'Neal Schaffer',
    'amsive.com': 'Amsive',
    'thedigitalbloom.com': 'The Digital Bloom',
    'lagrowthmachine.com': 'La Growth Machine',
    'b2bcontentos.com': 'B2B Content OS',
    'kalungi.com': 'Kalungi',
    'naganamedia.com': 'Nagana Media',
    'growthmindedmarketing.com': 'Growth Minded Marketing',
    'web.dev': 'web.dev',
    'developer.mozilla.org': 'MDN',
    'www.w3.org': 'W3C',
    'w3.org': 'W3C',
    'bing.com': 'Bing',
    'www.bing.com': 'Bing',
    'support.claude.com': 'Anthropic',
    'privacy.claude.com': 'Anthropic',
    'developers.openai.com': 'OpenAI',
    'platform.openai.com': 'OpenAI',
}

# tables that carry source_org (+ whether they carry source_url)
TABLES = [('rules', True), ('principles', True), ('anti_patterns', True),
          ('documents', True), ('playbooks', False)]


def main():
    if not DB_URL:
        sys.exit('SIEVE_DB_URL / DATABASE_URL not set')
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    stamp = date.today().strftime('%Y%m%d')

    # --- alias table ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sieve.org_aliases (
            alias text PRIMARY KEY, canonical_org text NOT NULL)
    """)
    for alias, canon in ALIASES.items():
        cur.execute("""
            INSERT INTO sieve.org_aliases (alias, canonical_org) VALUES (%s,%s)
            ON CONFLICT (alias) DO UPDATE SET canonical_org=EXCLUDED.canonical_org
        """, (alias.lower(), canon))
    print(f'org_aliases seeded: {len(ALIASES)} entries')

    for tbl, has_url in TABLES:
        # --- backup (once per day per table) ---
        bak = f'sieve.bak_org_{tbl}_{stamp}'
        cur.execute(f"SELECT to_regclass('{bak}')")
        if cur.fetchone()[0] is None:
            cur.execute(f"CREATE TABLE {bak} AS SELECT id, source_org FROM sieve.{tbl}")
            print(f'{tbl}: backup {bak} written')

        # --- pass 1: alias canonicalization ---
        cur.execute(f"""
            UPDATE sieve.{tbl} t SET source_org=a.canonical_org
            FROM sieve.org_aliases a
            WHERE lower(trim(t.source_org))=a.alias AND t.source_org<>a.canonical_org
        """)
        p1 = cur.rowcount

        p2 = 0
        if has_url:
            # --- pass 2: derive org from URL host for junk-org rows.
            # Sanity guard: only for plausible hosts, else a garbage URL like
            # 'https://' derives '' and escapes pass-3 quarantine forever.
            cur.execute(f"""
                UPDATE sieve.{tbl} SET source_org=lower(regexp_replace(
                    regexp_replace(source_url,'^https?://(www\\.)?',''),'/.*$',''))
                WHERE coalesce(source_org,'') = ANY(%s)
                  AND source_url ~ '^https?://[^/]+\\.[a-z]{{2,}}'
            """, (list(JUNK_ORGS),))
            p2 = cur.rowcount
            # re-canonicalize freshly derived domains
            cur.execute(f"""
                UPDATE sieve.{tbl} t SET source_org=a.canonical_org
                FROM sieve.org_aliases a
                WHERE lower(trim(t.source_org))=a.alias AND t.source_org<>a.canonical_org
            """)
            p1 += cur.rowcount

        # --- pass 3: quarantine what cannot be attributed ---
        if has_url:
            cur.execute(f"""
                UPDATE sieve.{tbl} SET source_org='unattributed-legacy'
                WHERE coalesce(source_org,'') = ANY(%s)
                  AND (source_url IS NULL OR source_url=''
                       OR source_url !~ '^https?://[^/]+\\.[a-z]{{2,}}')
            """, (list(JUNK_ORGS),))
        else:
            cur.execute(f"""
                UPDATE sieve.{tbl} SET source_org='unattributed-legacy'
                WHERE coalesce(source_org,'') = ANY(%s)
            """, (list(JUNK_ORGS),))
        p3 = cur.rowcount
        print(f'{tbl}: aliased={p1} derived-from-url={p2} quarantined={p3}')

    # summary
    cur.execute("""
        SELECT coalesce(source_org,'(null)'), count(*) FROM sieve.rules
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12
    """)
    print('\ntop orgs after normalization:')
    for org, n in cur.fetchall():
        print(f'  {org:32s} {n}')
    conn.close()


if __name__ == '__main__':
    main()

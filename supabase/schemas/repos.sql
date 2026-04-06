-- =============================================================================
-- GitBiz: repos table
-- Stores discovered GitHub repositories and their LLM-evaluated opportunity data.
-- =============================================================================

create table public.repos (
    id bigint generated always as identity primary key,
    name text not null,
    full_name text not null,
    url text not null unique,
    description text,
    stars integer not null default 0,
    language text,
    last_updated timestamptz,
    processed boolean not null default false,
    llm_status text check (llm_status in ('KEEP', 'REJECT')),
    score real,
    output_json jsonb,
    discord_posted boolean not null default false,
    created_at timestamptz not null default now()
);

comment on table public.repos is 'GitHub repositories discovered and evaluated for business/product opportunities.';

-- Unique index on url for fast dedup lookups
create unique index idx_repos_url on public.repos using btree (url);

-- Index for fetching unprocessed repos
create index idx_repos_unprocessed on public.repos using btree (processed) where processed = false;

-- Index for fetching top-scored KEEP repos not yet posted
create index idx_repos_keep_unposted on public.repos using btree (score desc)
    where llm_status = 'KEEP' and discord_posted = false;

-- =============================================================================
-- Row Level Security
-- =============================================================================
alter table public.repos enable row level security;

-- The bot operates via the service role key which bypasses RLS.
-- These policies cover the anon and authenticated roles for optional
-- dashboard / read access.

-- SELECT: authenticated users can read all repos
create policy "Authenticated users can read repos"
    on public.repos
    for select
    to authenticated
    using (true);

-- SELECT: anon users can read all repos (public dashboard scenario)
create policy "Anon users can read repos"
    on public.repos
    for select
    to anon
    using (true);

-- INSERT: only authenticated users (or service role) can insert
create policy "Authenticated users can insert repos"
    on public.repos
    for insert
    to authenticated
    with check (true);

-- UPDATE: only authenticated users (or service role) can update
create policy "Authenticated users can update repos"
    on public.repos
    for update
    to authenticated
    using (true)
    with check (true);

-- DELETE: only authenticated users (or service role) can delete
create policy "Authenticated users can delete repos"
    on public.repos
    for delete
    to authenticated
    using (true);

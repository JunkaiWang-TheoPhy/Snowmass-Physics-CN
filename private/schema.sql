-- Private authorization backend schema for PostgreSQL/Supabase.
-- This file contains no personal data. All tables enable RLS with no public
-- policies; browser/anon access therefore remains denied by default.

create extension if not exists pgcrypto;

create table if not exists papers (
    record_id text primary key,
    paper_id text not null unique,
    source_url text not null,
    source_version text,
    source_license text not null,
    permits_adaptation boolean,
    translation_status text not null default 'not-started'
        check (translation_status in (
            'not-started', 'machine-draft', 'human-review',
            'published', 'superseded', 'withdrawn'
        )),
    authorization_status text not null default 'not-reviewed'
        check (authorization_status in (
            'not-reviewed', 'license-cleared', 'needs-permission',
            'contacted', 'response-pending', 'permission-granted',
            'license-scope-verified', 'permission-denied', 'unclear', 'withdrawn'
        )),
    publication_allowed boolean not null default false,
    publication_basis text not null default 'manual-hold'
        check (publication_basis in (
            'source-license', 'permission-granted', 'public-domain',
            'manual-hold', 'denied'
        )),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (
        publication_allowed = false
        or publication_basis in ('source-license', 'permission-granted', 'public-domain')
    )
);

create table if not exists contacts (
    contact_id uuid primary key default gen_random_uuid(),
    record_id text not null references papers(record_id) on delete restrict,
    name text not null,
    role text not null
        check (role in (
            'corresponding-author', 'collaboration-contact',
            'rights-holder', 'publisher'
        )),
    institution text,
    email text not null,
    source_url text not null,
    source_type text not null
        check (source_type in (
            'paper', 'author-page', 'collaboration-page', 'publisher-page'
        )),
    verification_status text not null default 'unverified'
        check (verification_status in ('unverified', 'verified', 'stale', 'bounced', 'suppressed')),
    verified_at timestamptz,
    suppressed_at timestamptz,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (record_id, email)
);

create table if not exists authorization_requests (
    request_id uuid primary key default gen_random_uuid(),
    record_id text not null references papers(record_id) on delete restrict,
    contact_id uuid not null references contacts(contact_id) on delete restrict,
    campaign_id text not null,
    request_type text not null default 'translation-and-publication'
        check (request_type = 'translation-and-publication'),
    requested_scope jsonb not null,
    requested_license text,
    status text not null default 'draft'
        check (status in (
            'draft', 'pending-review', 'approved-to-send', 'sent',
            'delivered', 'bounced', 'replied', 'closed', 'cancelled'
        )),
    draft_subject text,
    draft_body text,
    approved_by text,
    approved_at timestamptz,
    first_sent_at timestamptz,
    last_event_at timestamptz,
    follow_up_due_at timestamptz,
    permission_decision text not null default 'unknown'
        check (permission_decision in (
            'unknown', 'granted', 'granted-with-conditions',
            'denied', 'redirected', 'unclear', 'no-response'
        )),
    permission_scope jsonb,
    evidence_object_key text,
    evidence_sha256 text,
    evidence_received_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (
        status not in ('approved-to-send', 'sent', 'delivered', 'bounced', 'replied', 'closed')
        or (approved_by is not null and approved_at is not null)
    ),
    check (permission_decision not in ('granted', 'granted-with-conditions') or evidence_sha256 is not null)
);

create unique index if not exists authorization_requests_active_dedup
    on authorization_requests (record_id, contact_id, request_type)
    where status not in ('closed', 'cancelled', 'bounced');

create table if not exists authorization_events (
    event_id uuid primary key default gen_random_uuid(),
    request_id uuid not null references authorization_requests(request_id) on delete restrict,
    event_type text not null,
    actor_type text not null check (actor_type in ('human', 'agent', 'system')),
    actor_id text not null,
    occurred_at timestamptz not null default now(),
    old_status text,
    new_status text,
    metadata jsonb not null default '{}'::jsonb
);

create or replace function reject_authorization_event_mutation()
returns trigger
language plpgsql
as $$
begin
    raise exception 'authorization_events is append-only';
end;
$$;

drop trigger if exists authorization_events_append_only on authorization_events;
create trigger authorization_events_append_only
before update or delete on authorization_events
for each row execute function reject_authorization_event_mutation();

alter table papers enable row level security;
alter table contacts enable row level security;
alter table authorization_requests enable row level security;
alter table authorization_events enable row level security;

comment on table contacts is
    'Private contact data. Never expose through the public site or generated manifest.';
comment on table authorization_requests is
    'Human-approved authorization request queue. approved-to-send requires approver evidence.';
comment on table authorization_events is
    'Append-only private audit log; public sync receives redacted state only.';

begin;

-- Keep the original tables as a private rollback copy. Wait for any legacy
-- writers before copying so that the new library contains their last commits.
lock table public.glossaries, public.translation_memory in share row exclusive mode;

create table public.knowledge_project_members (
    game_id text not null,
    user_id uuid not null references auth.users(id) on delete cascade,
    can_edit boolean not null default false,
    primary key (game_id, user_id)
);

create function public.knowledge_scope(scene text, string_key text) returns text
language sql immutable strict set search_path = ''
as $$ select case when string_key <> '' then 'key:' || string_key else 'scene:' || scene end $$;

create table public.knowledge_entries (
    id uuid primary key default gen_random_uuid(),
    game_id text not null,
    target_language text not null,
    kind text not null check (kind in ('terms', 'memory')),
    source text not null check (source <> ''),
    target text not null check (target <> ''),
    scene text not null default '',
    string_key text not null default '',
    note text not null default '',
    status text not null check (status in ('active', 'pending', 'archived')),
    origin text not null check (origin in ('legacy', 'manual', 'paste', 'file')),
    verified boolean not null default false check (not verified or status = 'active'),
    version bigint not null default 1,
    updated_at timestamptz not null default now(),
    source_hash text generated always as (md5(source)) stored,
    scope_hash text generated always as (md5(public.knowledge_scope(scene, string_key))) stored,
    check (kind = 'memory' or string_key = ''),
    unique (game_id, target_language, kind, source_hash, scope_hash)
);

create index knowledge_entries_recent
    on public.knowledge_entries (game_id, target_language, kind, updated_at desc, id);
create index knowledge_entries_active
    on public.knowledge_entries (game_id, target_language, id) where status = 'active';
create index knowledge_project_members_user on public.knowledge_project_members (user_id);

insert into public.knowledge_entries (game_id, target_language, kind, source, target, note, status, origin, updated_at)
select game_id, target_language, 'terms', source_term, target_term, coalesce(note, ''), 'active', 'legacy',
       coalesce(updated_at, now())
from public.glossaries;

insert into public.knowledge_entries (id, game_id, target_language, kind, source, target, status, origin)
select id, game_id, target_language, 'memory', source_text, target_text, 'active', 'legacy'
from public.translation_memory;

alter table public.knowledge_project_members enable row level security;
alter table public.knowledge_entries enable row level security;

revoke all on public.knowledge_project_members, public.knowledge_entries,
    public.glossaries, public.translation_memory from public, anon, authenticated;
grant select on public.knowledge_project_members, public.knowledge_entries to authenticated;
grant insert (game_id, target_language, kind, source, target, scene, string_key, note, status, origin, verified),
    update (source, target, scene, string_key, note, status, verified), delete
    on public.knowledge_entries to authenticated;

create policy knowledge_members_self on public.knowledge_project_members for select to authenticated
    using (user_id = (select auth.uid()));
create policy knowledge_read on public.knowledge_entries for select to authenticated
    using (game_id in (select m.game_id from public.knowledge_project_members m where m.user_id = (select auth.uid())));
create policy knowledge_insert on public.knowledge_entries for insert to authenticated
    with check (game_id in (select m.game_id from public.knowledge_project_members m where m.user_id = (select auth.uid()) and m.can_edit));
create policy knowledge_update on public.knowledge_entries for update to authenticated
    using (game_id in (select m.game_id from public.knowledge_project_members m where m.user_id = (select auth.uid()) and m.can_edit))
    with check (game_id in (select m.game_id from public.knowledge_project_members m where m.user_id = (select auth.uid()) and m.can_edit));
create policy knowledge_delete on public.knowledge_entries for delete to authenticated
    using (game_id in (select m.game_id from public.knowledge_project_members m where m.user_id = (select auth.uid()) and m.can_edit));

create function public.knowledge_entry_changed() returns trigger
language plpgsql set search_path = ''
as $$
begin
    new.version := old.version + 1;
    new.updated_at := clock_timestamp();
    return new;
end;
$$;

create trigger knowledge_entry_changed before update on public.knowledge_entries
    for each row execute function public.knowledge_entry_changed();

create function public.knowledge_list(
    p_game text, p_language text, p_kind text, p_query text, p_scene text, p_status text,
    p_offset integer, p_limit integer
) returns jsonb
language sql stable security invoker set search_path = ''
as $$
    with collection as not materialized (
        select id, source, target, scene, string_key, note, status, origin, verified, version, updated_at
        from public.knowledge_entries
        where game_id = p_game and target_language = p_language and kind = p_kind
    ), matches as not materialized (
        select * from collection
        where (case when p_status = 'current' then status <> 'archived' else status = p_status end)
          and (p_scene = '' or scene = p_scene)
          and (p_query = '' or strpos(lower(source), lower(p_query)) > 0
               or strpos(lower(target), lower(p_query)) > 0 or strpos(lower(string_key), lower(p_query)) > 0)
    )
    select jsonb_build_object(
        'total', (select count(*) from matches),
        'rows', coalesce((select jsonb_agg(to_jsonb(r) order by r.updated_at desc, r.id)
                          from (select * from matches order by updated_at desc, id
                                limit least(greatest(p_limit, 1), 100) offset greatest(p_offset, 0)) r), '[]'::jsonb),
        'scenes', coalesce((select jsonb_agg(distinct scene order by scene) from collection where scene <> ''), '[]'::jsonb)
    )
$$;

create function public.knowledge_import_matches(p_game text, p_language text, p_kind text, p_rows jsonb)
returns table (line integer, target text, note text, scene text)
language sql stable security invoker set search_path = ''
as $$
    select incoming.line, e.target, e.note, e.scene
    from jsonb_to_recordset(p_rows) as incoming(line integer, source text, scene text, string_key text)
    join public.knowledge_entries e on e.game_id = p_game and e.target_language = p_language and e.kind = p_kind
       and e.source_hash = md5(incoming.source)
       and e.scope_hash = md5(public.knowledge_scope(incoming.scene, incoming.string_key))
$$;

create function public.knowledge_mutate(
    p_game text, p_language text, p_kind text, p_action text, p_rows jsonb, p_refs jsonb, p_status text, p_origin text
) returns integer
language plpgsql security invoker set search_path = ''
as $$
declare
    affected integer;
    entry jsonb;
begin
    if not exists (select 1 from public.knowledge_project_members
                   where game_id = p_game and user_id = (select auth.uid()) and can_edit) then
        raise sqlstate 'PT403' using message = 'No knowledge write permission';
    end if;

    if p_action = 'create' or p_action = 'import' then
        insert into public.knowledge_entries
            (game_id, target_language, kind, source, target, scene, string_key, note, status, origin, verified)
        select p_game, p_language, p_kind, r.source, r.target, r.scene, r.string_key, r.note, p_status, p_origin,
               p_status = 'active'
        from jsonb_to_recordset(p_rows) as r(source text, target text, scene text, string_key text, note text)
        on conflict (game_id, target_language, kind, source_hash, scope_hash) do nothing;
        get diagnostics affected = row_count;
        if p_action = 'create' and affected <> 1 then
            raise sqlstate 'PT409' using message = 'Duplicate knowledge entry';
        end if;
        return affected;
    end if;

    if p_action not in ('edit', 'status', 'delete') or jsonb_array_length(p_refs) not between 1 and 1000 then
        raise sqlstate 'PT400' using message = 'Invalid knowledge change';
    end if;

    -- Lock all requested versions in a consistent order. A stale or foreign
    -- row rejects the batch before any of its changes can be written.
    perform e.id from public.knowledge_entries e
    join jsonb_to_recordset(p_refs) as r(id uuid, version bigint) on r.id = e.id and r.version = e.version
    where e.game_id = p_game and e.target_language = p_language and e.kind = p_kind
    order by e.id for update of e;
    get diagnostics affected = row_count;
    if affected <> jsonb_array_length(p_refs) then
        raise sqlstate 'PT409' using message = 'Knowledge entry changed';
    end if;

    if p_action = 'edit' then
        entry := p_rows -> 0;
        update public.knowledge_entries
        set source = entry ->> 'source', target = entry ->> 'target', scene = entry ->> 'scene',
            string_key = entry ->> 'string_key', note = entry ->> 'note', status = p_status,
            verified = (p_status = 'active')
        where id = (p_refs -> 0 ->> 'id')::uuid;
    elsif p_action = 'status' then
        update public.knowledge_entries
        set status = p_status, verified = (p_status = 'active')
        where id in (select r.id from jsonb_to_recordset(p_refs) as r(id uuid))
          and (status, verified) is distinct from (p_status, p_status = 'active');
    else
        delete from public.knowledge_entries
        where id in (select r.id from jsonb_to_recordset(p_refs) as r(id uuid));
    end if;
    get diagnostics affected = row_count;
    return affected;
end;
$$;

revoke all on function public.knowledge_entry_changed() from public, anon, authenticated;
revoke all on function public.knowledge_scope(text, text),
    public.knowledge_list(text, text, text, text, text, text, integer, integer),
    public.knowledge_import_matches(text, text, text, jsonb),
    public.knowledge_mutate(text, text, text, text, jsonb, jsonb, text, text)
    from public, anon, authenticated;
grant execute on function public.knowledge_scope(text, text),
    public.knowledge_list(text, text, text, text, text, text, integer, integer),
    public.knowledge_import_matches(text, text, text, jsonb),
    public.knowledge_mutate(text, text, text, text, jsonb, jsonb, text, text) to authenticated;

commit;

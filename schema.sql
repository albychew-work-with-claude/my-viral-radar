-- ============================================================
-- Viral Radar · Supabase table setup
--
-- ✅ This version is safe to re-run: it never deletes any data.
--    (The old version started with "drop table", so one re-run =
--     all posts / transcripts / AI breakdowns / statuses gone.)
--
-- Paste the whole thing into Supabase → SQL Editor → Run.
-- Seeing "Success" means it worked.
-- ============================================================

-- ---------- Viral posts ----------
create table if not exists posts (
  id              bigint generated always as identity primary key,
  post_id         text unique not null,
  tracker         text not null default 'IG',
  competitor      text,
  caption         text,
  transcript      text,                            -- Video voiceover script (auto-transcribed)
  ai_breakdown    text,                            -- AI breakdown (auto-generated)
  my_script       text,                            -- My rewritten script (AI rewrites it using brand_voice.md; ready to film)
  post_type       text,
  likes           integer default 0,
  comments        integer default 0,
  followers       integer default 0,
  engagement_rate numeric generated always as
      ((likes + comments)::numeric / nullif(followers, 0)) stored,
  viral_score     numeric generated always as
      (round((greatest(likes, 0) + comments * 3)::numeric / nullif(followers, 0) * 100, 2)) stored,
  post_date       date,
  post_url        text,
  thumbnail_url   text,
  video_url       text,
  hashtags        text,
  is_video        boolean default false,
  status          text default 'New',               -- New / Filming / Done / Skipped
  last_synced     date,
  created_at      timestamptz default now()
);

-- ---------- Competitor list (self-managed: add a row = track it; untick active = stop) ----------
create table if not exists competitors (
  id         bigint generated always as identity primary key,
  username   text not null,
  tracker    text not null default 'IG',
  active     boolean default true,
  notes      text,
  created_at timestamptz default now()
);

-- ============================================================
-- Data clean-up + constraints (needed when upgrading from an old version;
-- harmless on a fresh install)
-- ============================================================

-- Normalise competitor usernames: strip the @ and leading/trailing spaces
update competitors
   set username = ltrim(btrim(username), '@')
 where username <> ltrim(btrim(username), '@');

-- Remove duplicate competitors (a duplicate = the same account scraped twice
-- per sync = paying Apify double)
delete from competitors a
 using competitors b
 where a.id > b.id
   and lower(a.username) = lower(b.username)
   and a.tracker = b.tracker;

-- Block duplicates from being inserted in future
create unique index if not exists competitors_username_uniq
    on competitors (lower(username), tracker);

-- Old-version upgrade: add the "My Script" column (fresh installs already
-- have it; re-running has no side effects)
alter table posts add column if not exists my_script text;

-- Status allow-list: the public key can edit this column, so without a lock
-- anyone could stuff any amount of text into it.
-- The old lock only allowed the Chinese labels, so it is removed first.
alter table posts drop constraint if exists posts_status_chk;

-- Old-version upgrade: convert the old Chinese status labels to English
-- (nothing to convert on a fresh install; re-running has no side effects)
update posts set status = case status
         when '未处理' then 'New'
         when '拍摄中' then 'Filming'
         when '已处理' then 'Done'
         when '跳过'   then 'Skipped'
       end
 where status in ('未处理', '拍摄中', '已处理', '跳过');

update posts set status = 'New'
 where status is null or status not in ('New', 'Filming', 'Done', 'Skipped');

alter table posts alter column status set default 'New';

alter table posts add  constraint posts_status_chk
      check (status in ('New', 'Filming', 'Done', 'Skipped'));

-- Old-version upgrade: convert the old Chinese "failed / empty" markers
-- to English (re-running has no side effects)
update posts set transcript = case transcript
         when '(转录失败)'   then '(transcription failed)'
         when '(视频不可用)' then '(video unavailable)'
         when '(无口播内容)' then '(no speech)'
       end
 where transcript in ('(转录失败)', '(视频不可用)', '(无口播内容)');
update posts set ai_breakdown = '(breakdown failed)' where ai_breakdown = '(拆解失败)';
update posts set my_script    = '(rewrite failed)'   where my_script    = '(改写失败)';

-- Old auto-suggested competitors (never ticked active) had Chinese notes;
-- remove them so the next sync refreshes the shortlist in English
delete from competitors where active = false and notes like '系统推荐%';

create index if not exists posts_status_idx     on posts (status);
create index if not exists posts_score_idx      on posts (viral_score desc nulls last);
create index if not exists posts_competitor_idx on posts (competitor);
create index if not exists posts_synced_idx     on posts (last_synced);

-- ============================================================
-- Security lock (important!): the browser's public key can only
-- [read posts] + [edit the status column].
-- You must REVOKE ALL first. By default Supabase gives the public key full
-- permissions; if you don't remove them, anyone with the page link could
-- delete all your data.
-- ============================================================
alter table posts       enable row level security;
alter table competitors enable row level security;

revoke all on posts       from anon;
revoke all on competitors from anon;

grant select on posts to anon;
grant update (status) on posts to anon;

drop policy if exists "anon read posts"    on posts;
drop policy if exists "anon update status" on posts;
create policy "anon read posts"    on posts for select to anon using (true);
create policy "anon update status" on posts for update to anon using (true) with check (true);

-- ============================================================
-- Competitor list: already filled in with the accounts you gave.
-- Paste the whole thing and Run; no changes needed.
-- To add / stop competitors later: Supabase → Table Editor → competitors table.
-- ============================================================
insert into competitors (username, tracker, active) values
  ('jacqchowfinance', 'IG', true),
  ('sissigohlegacyplanner', 'IG', true),
  ('olivia_liwenjun', 'IG', true)
on conflict do nothing;

-- ============================================================
-- For adding competitors only. Run just this block; never re-run the
-- whole of schema.sql for this.
-- Usernames: the plain account name only, no @, no link.
-- Re-running is safe and will not create duplicates.
-- ============================================================
insert into competitors (username, tracker, active) values
  ('new_competitor_1', 'IG', true),
  ('new_competitor_2', 'IG', true),
  ('new_competitor_3', 'IG', true)
on conflict do nothing;

-- To stop tracking a competitor (keeps its data, just stops scraping):
-- update competitors set active = false where lower(username) = lower('account_to_stop');

-- To see who you are tracking now:
-- select username, tracker, active, notes from competitors order by active desc, username;

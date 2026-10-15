alter table public.messages drop constraint messages_kind_check;
alter table public.messages add constraint messages_kind_check check (kind in (
  'prompt', 'questions', 'answers', 'concepts', 'concept_pick', 'tweak', 'tweak_decision',
  'mesh_review', 'mesh_decision', 'result', 'review_request', 'review_decision', 'note', 'resume',
  'spool_suggestion'));

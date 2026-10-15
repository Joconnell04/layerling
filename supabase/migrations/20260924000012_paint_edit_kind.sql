-- User message kind paint_edit (contracts payloads.PaintEdit): repaint a finished job by region id
-- and by face set from the studio paint editor or the paint MCP server. Not applied yet.
alter table public.messages drop constraint messages_kind_check;
alter table public.messages add constraint messages_kind_check check (kind in (
  'prompt', 'questions', 'answers', 'concepts', 'concept_pick', 'tweak', 'tweak_decision',
  'mesh_review', 'mesh_decision', 'result', 'review_request', 'review_decision', 'note', 'resume',
  'spool_suggestion', 'palette_edit', 'paint_edit'));

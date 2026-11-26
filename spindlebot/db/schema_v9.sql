-- Freeze the source integrity expected by each COPY plan. Existing actions
-- deliberately remain NULL: today's presence cannot reconstruct an old plan.
-- The executor refuses those copies and asks for inventory + a new review.
ALTER TABLE pending_action ADD COLUMN expected_sha256 TEXT;

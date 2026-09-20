-- A host is claimed by an account; it keeps its identity. host_id is baked
-- into every session id, so replacing it with an account id would fork the
-- entire history instead of linking this machine to its owner.
--
-- NULL keeps an install that never signs in perfectly valid: local-first is
-- the default. Account, identity and team tables belong to the server, so a
-- local host must not depend on a foreign key into that separate store.
ALTER TABLE host ADD COLUMN account_id TEXT;

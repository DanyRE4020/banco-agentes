-- MigraciÃ³n idempotente. Solo agrega docs; no altera datos de negocio.
CREATE SCHEMA IF NOT EXISTS docs;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA docs;
DO $$ BEGIN
 IF (SELECT extnamespace FROM pg_extension WHERE extname='vector') <> 'docs'::regnamespace THEN
  ALTER EXTENSION vector SET SCHEMA docs;
 END IF;
END $$;
REVOKE ALL ON SCHEMA docs FROM PUBLIC;
CREATE TABLE IF NOT EXISTS docs.fragments (
 id text PRIMARY KEY,
 corpus text NOT NULL CHECK (corpus IN ('bank_documents','cards_dictionary')),
 file text NOT NULL,
 file_hash text NOT NULL,
 text text NOT NULL,
 source jsonb NOT NULL,
 model_identity text NOT NULL,
 embedding docs.vector(1024) NOT NULL,
 indexed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fragments_corpus_file ON docs.fragments(corpus,file);
REVOKE ALL ON ALL TABLES IN SCHEMA docs FROM PUBLIC;
GRANT USAGE ON SCHEMA docs TO :"app_user";
GRANT SELECT ON ALL TABLES IN SCHEMA docs TO :"app_user";

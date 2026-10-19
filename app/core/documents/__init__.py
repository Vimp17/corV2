"""Document pipeline: Excel / PDF / scans and photos -> text -> document type -> fields -> data.

Stages (app.core.documents.pipeline.process_document):
  intake    - a file arrives (upload or inbox folder), is checked, stored and queued;
  text      - Excel matching a source profile goes to the existing import; other Excel sheets,
              PDF text layers and OCR of scanned pages / photos (LiteLLM vision) give the text;
  extract   - the document type is found by keywords (LLM if none match) and the fields of that
              type are extracted by the LLM, each with a quote that must occur in the text;
  load      - checked records are written to the usual telemetry / history / passport data and
              the rating is recomputed. A document that is not recognised loads nothing: its
              data counts as missing.
"""

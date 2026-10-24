"""The evaluation dataset generator and runner (step 7).

A package the application does **not** import. That is the point: the generator
decides what counts as fraud, and if it could reach into the detectors to do so
the whole measurement would be circular.

## The independence rule

**No module in this package may import or call any detector, validator or
pipeline code to decide what a case is.** Labels come from the recipe that
built each document - the generator knows it edited a digit because it edited
the digit, not because a checker told it so.

The runner is the one place that touches `app`, and only in two narrow ways:

- it calls the real pipeline entry point `process_document()` to produce
  results, and
- it reads results back out of the database to compare against labels.

It never asks a detector what a document is. A test enforces the boundary by
scanning this package's imports.

## Entry points

```
python -m evaluation.generate     # build the dataset, manifest and labels
python -m evaluation.run          # run the pipeline over it and score
python -m evaluation.distances    # re-measure the perceptual-hash thresholds
```

Everything in `data/eval/` is generated and gitignored. Only the code and the
two config files are committed.
"""

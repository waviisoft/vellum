# Map

Where things are in this repo, and the standing decisions behind them. Area
notes live in `.vellum/memory/areas/`; wave worklogs in `.vellum/memory/waves/`.

Seeded by `vellum init`. It is a skeleton on purpose: a map written ahead of the
repository it describes is a second place for the layout to drift. Fill it in as
the first wave lands, and keep every claim in it something a reader can grep for.

## Layout

| Path | What |
|---|---|
| `src/` | The product. |
| `.vellum/product.yaml` | Backref to `{intent_slug}`, and the pin of record — `pin.commit`. |
| `.vellum/memory/` | This map, area notes, and one worklog per wave. |

## Areas

None yet. An area note is the durable answer to "how does this part work and
why"; exit duty is what keeps them true — a work item is done only when the
implementer updated the area notes it touched.

## Waves

None yet.

## Technology choice, and why

Not chosen yet. Record it here when it is, with the reasoning, so a later wave
can re-open the decision knowingly rather than by accident.

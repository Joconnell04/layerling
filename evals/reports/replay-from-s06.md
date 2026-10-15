# Replay from s06

Claude recorded, s09 paint replay, Meshy replay (0 credits). Stored run numbers in brackets are SOURCE: the job's manifests and slice reports; rerun numbers are SOURCE: this run.

| Job | Status | Gate | Form | Concept check | Final check | Slots | Print time | Grams | s06 wall | s06 output | Meshy calls |
|---|---|---|---|---|---|---|---|---|---|---|---|
| sombrero | done | Bambu pass | figure | pass (stored) | fail, approved (stored) | 5 (5) | 7 h 02 min (7 h 02 min) | 111.49 (111.49) | 19 s (8.9 min) | identical | 2 (2 hash), 0 missed, 0 cr |
| catfish | done | Bambu pass | flat | pass (stored) | fail, approved (stored) | 4 (4) | 0 h 54 min (0 h 54 min) | 10.89 (10.89) | 15 s (34 s) | differs | 2 (2 hash), 0 missed, 0 cr |
| bronco | done | Bambu pass | figure | pass (stored) | pass, approved (stored) | 4 (4) | 6 h 01 min (6 h 01 min) | 104.61 (104.61) | 4 s (17.9 min) | identical | 2 (2 hash), 0 missed, 0 cr |
| frenchie | done | Bambu pass | figure (stored flat) | - (stored) | - | 2 (3) | 2 h 01 min (0 h 39 min) | 20.11 (9.54) | 8 s (101 s) | differs | 2 (2 hash), 0 missed, 0 cr |
| bronco_m3 | done | Bambu pass | figure | - (stored) | - | 3 (3) | 9 h 17 min (9 h 22 min) | 126.34 (127.15) | 10 s (43 s) | differs | 2 (2 hash), 0 missed, 0 cr |

Stage wall times, rerun (stored):

| Job | s01 | s02 | s03 | s04 | s05 | s06 | s07 | s08 | s09 | s10 | Total |
|---|---|---|---|---|---|---|---|---|---|---|---|
| sombrero | - | - | - | - | - | 19 s (8.9 min) | 30 s (3.0 min) | 26 s (32 s) | 0 s (18.3 min) | 115 s (8.8 min) | 3.2 min |
| catfish | - | - | - | - | - | 15 s (34 s) | 11 s (32 s) | 9 s (12 s) | 0 s (710.6 min) | 2.7 min (3.4 min) | 3.3 min |
| bronco | - | - | - | - | - | 4 s (17.9 min) | 13 s (56 s) | 13 s (29 s) | 0 s (7.2 min) | 55 s (105 s) | 85 s |
| frenchie | - | - | - | - | - | 8 s (101 s) | 95 s (72 s) | - | - | - | 103 s |
| bronco_m3 | - | - | - | - | - | 10 s (43 s) | 67 s (25 s) | - | - | - | 76 s |

Notes:

- catfish: Claude miss at s06_printability (concept_read)
- frenchie: Claude miss at s06_printability (concept_read)
- bronco_m3: Claude miss at s06_printability (concept_read)

Suite wall time 10.9 min. Stages before the start are the stored run's (-). Stored s01 to s05 wall times include customer gate waits and Meshy queues; stored s09 is the paint agent.

Catfish "differs": its concept read was recorded in a stale s06 attempt the first store left out, so the add-on color fell back (keyring black, not the concept's silver); meshes identical. The store now keeps every attempt's call bodies; rerun pending.

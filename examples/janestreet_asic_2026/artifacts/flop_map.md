# Flop map (92 flip-flops)

| group | role | Q net | cell | instance | x (um) | y (um) |
|---|---|---|---|---|---|---|
| bit counter (column, 0..10) | `col[0]` | `n86` | dfrtp | `dfrtp_2_26220_149600` | 26.22 | 149.60 |
| bit counter (column, 0..10) | `col[1]` | `n6` | dfrtp | `dfrtp_2_30820_146880` | 30.82 | 146.88 |
| bit counter (column, 0..10) | `col[2]` | `n93` | dfrtp | `dfrtp_2_26220_144160` | 26.22 | 144.16 |
| bit counter (column, 0..10) | `col[3]` | `n182` | dfrtp | `dfrtp_2_28980_157760` | 28.98 | 157.76 |
| character counter (row, 0..10) | `row[0]` | `n87` | dfrtp | `dfrtp_2_32200_92480` | 32.20 | 92.48 |
| character counter (row, 0..10) | `row[1]` | `n89` | dfrtp | `dfrtp_2_33120_103360` | 33.12 | 103.36 |
| character counter (row, 0..10) | `row[2]` | `n88` | dfrtp | `dfrtp_2_28980_108800` | 28.98 | 108.80 |
| character counter (row, 0..10) | `row[3]` | `n102` | dfrtp | `dfrtp_2_28980_97920` | 28.98 | 97.92 |
| population counter (total stars) | `pop[0]` | `n213` | dfrtp | `dfrtp_2_80960_54400` | 80.96 | 54.40 |
| population counter (total stars) | `pop[1]` | `n146` | dfrtp | `dfrtp_2_80960_38080` | 80.96 | 38.08 |
| population counter (total stars) | `pop[2]` | `n147` | dfrtp | `dfrtp_2_82800_43520` | 82.80 | 43.52 |
| population counter (total stars) | `pop[3]` | `n173` | dfrtp | `dfrtp_2_81880_48960` | 81.88 | 48.96 |
| population counter (total stars) | `pop[4]` | `n130` | dfrtp | `dfrtp_2_77740_35360` | 77.74 | 35.36 |
| population counter (total stars) | `pop[5]` | `n142` | dfrtp | `dfrtp_2_76820_40800` | 76.82 | 40.80 |
| population counter (total stars) | `pop[6]` | `n189` | dfrtp | `dfrtp_2_75900_46240` | 75.90 | 46.24 |
| population counter (total stars) | `pop[7]` | `n204` | dfrtp | `dfrtp_2_77740_57120` | 77.74 | 57.12 |
| per-row star checker | `row_prev (row count low bit)` | `n327` | dfrtp | `dfrtp_2_79120_108800` | 79.12 | 108.80 |
| per-row star checker | `row_two  (row count high bit)` | `n312` | dfrtp | `dfrtp_2_81880_97920` | 81.88 | 97.92 |
| per-row star checker | `row_bad  (sticky: some row != 2)` | `n284` | dfrtp | `dfrtp_2_80960_92480` | 80.96 | 92.48 |
| input delay line (adjacency window) | `sr[0]  = I one bit ago (left neighbour)` | `n468` | dfrtp | `dfrtp_2_81880_157760` | 81.88 | 157.76 |
| input delay line (adjacency window) | `sr[1]` | `n466` | dfrtp | `dfrtp_2_77740_155040` | 77.74 | 155.04 |
| input delay line (adjacency window) | `sr[2]` | `n475` | dfrtp | `dfrtp_2_76820_163200` | 76.82 | 163.20 |
| input delay line (adjacency window) | `sr[3]` | `n444` | dfrtp | `dfrtp_2_77740_149600` | 77.74 | 149.60 |
| input delay line (adjacency window) | `sr[4]` | `n415` | dfrtp | `dfrtp_2_74980_144160` | 74.98 | 144.16 |
| input delay line (adjacency window) | `sr[5]` | `n398` | dfrtp | `dfrtp_2_76820_138720` | 76.82 | 138.72 |
| input delay line (adjacency window) | `sr[6]` | `n402` | dfrtp | `dfrtp_2_75440_146880` | 75.44 | 146.88 |
| input delay line (adjacency window) | `sr[7]` | `n386` | dfrtp | `dfrtp_2_77740_133280` | 77.74 | 133.28 |
| input delay line (adjacency window) | `sr[8]` | `n393` | dfrtp | `dfrtp_2_81880_136000` | 81.88 | 136.00 |
| input delay line (adjacency window) | `sr[9]  = up-right neighbour` | `n405` | dfrtp | `dfrtp_2_82800_141440` | 82.80 | 141.44 |
| input delay line (adjacency window) | `sr[10] = up neighbour` | `n419` | dfrtp | `dfrtp_2_85100_146880` | 85.10 | 146.88 |
| input delay line (adjacency window) | `sr[11] = up-left neighbour` | `n456` | dfrtp | `dfrtp_2_84180_152320` | 84.18 | 152.32 |
| adjacency violation flag | `adj_bad (sticky)` | `n418` | dfrtp | `dfrtp_2_87860_149600` | 87.86 | 149.60 |
| message LFSR / input digest | `lfsr[0] (dfstp -> 1)` | `n509` | dfstp | `dfstp_2_172960_198560` | 172.96 | 198.56 |
| message LFSR / input digest | `lfsr[1]` | `n562` | dfrtp | `dfrtp_2_167900_195840` | 167.90 | 195.84 |
| message LFSR / input digest | `lfsr[2] (dfstp -> 1)` | `n524` | dfstp | `dfstp_2_167900_190400` | 167.90 | 190.40 |
| message LFSR / input digest | `lfsr[3]` | `n500` | dfrtp | `dfrtp_2_173880_193120` | 173.88 | 193.12 |
| message LFSR / input digest | `lfsr[4]` | `n513` | dfrtp | `dfrtp_2_174800_182240` | 174.80 | 182.24 |
| message LFSR / input digest | `lfsr[5] (dfstp -> 1)` | `n550` | dfstp | `dfstp_2_166980_187680` | 166.98 | 187.68 |
| message LFSR / input digest | `lfsr[6]` | `n522` | dfrtp | `dfrtp_2_168820_176800` | 168.82 | 176.80 |
| message LFSR / input digest | `lfsr[7] (dfstp -> 1)` | `n499` | dfstp | `dfstp_2_167900_179520` | 167.90 | 179.52 |
| phase control | `done (121 bits consumed)` | `n575` | dfrtp | `dfrtp_2_29900_201280` | 29.90 | 201.28 |
| phase control | `outphase (message streaming)` | `n665` | dfrtp | `dfrtp_2_167900_282880` | 167.90 | 282.88 |
| output byte counter | `outcnt[0]` | `n252` | dfxtp | `dfxtp_2_167900_247520` | 167.90 | 247.52 |
| output byte counter | `outcnt[1]` | `n245` | dfxtp | `dfxtp_2_168820_252960` | 168.82 | 252.96 |
| output byte counter | `outcnt[2]` | `n246` | dfxtp | `dfxtp_2_169740_250240` | 169.74 | 250.24 |
| output byte counter | `outcnt[3]` | `n291` | dfxtp | `dfxtp_2_168820_242080` | 168.82 | 242.08 |
| verdict | `success` | `success` | dfrtp | `dfrtp_2_172040_280160` | 172.04 | 280.16 |
| verdict | `near (all counts right, stars touch)` | `n680` | dfrtp | `dfrtp_2_167900_272000` | 167.90 | 272.00 |
| region star counter 0 | `reg_cnt[0][0]` | `n205` | dfrtp | `dfrtp_2_115000_48960` | 115.00 | 48.96 |
| region star counter 0 | `reg_cnt[0][1]` | `n176` | dfrtp | `dfrtp_2_118220_46240` | 118.22 | 46.24 |
| region star counters | `reg_cnt[1][0]` | `n223` | dfrtp | `dfrtp_2_115000_70720` | 115.00 | 70.72 |
| region star counters | `reg_cnt[1][1]` | `n218` | dfrtp | `dfrtp_2_114080_62560` | 114.08 | 62.56 |
| region star counters | `reg_cnt[2][0]` | `n225` | dfrtp | `dfrtp_2_115920_76160` | 115.92 | 76.16 |
| region star counters | `reg_cnt[2][1]` | `n240` | dfrtp | `dfrtp_2_113620_78880` | 113.62 | 78.88 |
| region star counters | `reg_cnt[3][0]` | `n242` | dfrtp | `dfrtp_2_115000_73440` | 115.00 | 73.44 |
| region star counters | `reg_cnt[3][1]` | `n231` | dfrtp | `dfrtp_2_115000_81600` | 115.00 | 81.60 |
| region star counters | `reg_cnt[4][0]` | `n288` | dfrtp | `dfrtp_2_113620_95200` | 113.62 | 95.20 |
| region star counters | `reg_cnt[4][1]` | `n264` | dfrtp | `dfrtp_2_115920_92480` | 115.92 | 92.48 |
| region star counters | `reg_cnt[5][0]` | `n271` | dfrtp | `dfrtp_2_115000_97920` | 115.00 | 97.92 |
| region star counters | `reg_cnt[5][1]` | `n301` | dfrtp | `dfrtp_2_123280_95200` | 123.28 | 95.20 |
| region star counters | `reg_cnt[6][0]` | `n331` | dfrtp | `dfrtp_2_115000_108800` | 115.00 | 108.80 |
| region star counters | `reg_cnt[6][1]` | `n334` | dfrtp | `dfrtp_2_118220_106080` | 118.22 | 106.08 |
| region star counters | `reg_cnt[7][0]` | `n355` | dfrtp | `dfrtp_2_114080_122400` | 114.08 | 122.40 |
| region star counters | `reg_cnt[7][1]` | `n352` | dfrtp | `dfrtp_2_115920_114240` | 115.92 | 114.24 |
| region star counters | `reg_cnt[8][0]` | `n365` | dfrtp | `dfrtp_2_114080_136000` | 114.08 | 136.00 |
| region star counters | `reg_cnt[8][1]` | `n363` | dfrtp | `dfrtp_2_115000_127840` | 115.00 | 127.84 |
| region star counters | `reg_cnt[9][0]` | `n376` | dfrtp | `dfrtp_2_115000_138720` | 115.00 | 138.72 |
| region star counters | `reg_cnt[9][1]` | `n378` | dfrtp | `dfrtp_2_118220_133280` | 118.22 | 133.28 |
| region star counters | `reg_cnt[10][0]` | `n407` | dfrtp | `dfrtp_2_115000_146880` | 115.00 | 146.88 |
| region star counters | `reg_cnt[10][1]` | `n422` | dfrtp | `dfrtp_2_118220_144160` | 118.22 | 144.16 |
| column star counters | `col_cnt[0][0]` | `n547` | dfrtp | `dfrtp_2_116380_184960` | 116.38 | 184.96 |
| column star counters | `col_cnt[0][1]` | `n533` | dfrtp | `dfrtp_2_114080_182240` | 114.08 | 182.24 |
| column star counters | `col_cnt[1][0]` | `n583` | dfrtp | `dfrtp_2_115920_206720` | 115.92 | 206.72 |
| column star counters | `col_cnt[1][1]` | `n584` | dfrtp | `dfrtp_2_113620_204000` | 113.62 | 204.00 |
| column star counters | `col_cnt[2][0]` | `n599` | dfrtp | `dfrtp_2_113160_212160` | 113.16 | 212.16 |
| column star counters | `col_cnt[2][1]` | `n604` | dfrtp | `dfrtp_2_115920_209440` | 115.92 | 209.44 |
| column star counters | `col_cnt[3][0]` | `n627` | dfrtp | `dfrtp_2_116380_217600` | 116.38 | 217.60 |
| column star counters | `col_cnt[3][1]` | `n619` | dfrtp | `dfrtp_2_114080_214880` | 114.08 | 214.88 |
| column star counters | `col_cnt[4][0]` | `n652` | dfrtp | `dfrtp_2_113620_231200` | 113.62 | 231.20 |
| column star counters | `col_cnt[4][1]` | `n654` | dfrtp | `dfrtp_2_115920_228480` | 115.92 | 228.48 |
| column star counters | `col_cnt[5][0]` | `n661` | dfrtp | `dfrtp_2_115920_239360` | 115.92 | 239.36 |
| column star counters | `col_cnt[5][1]` | `n660` | dfrtp | `dfrtp_2_113620_236640` | 113.62 | 236.64 |
| column star counters | `col_cnt[6][0]` | `n678` | dfrtp | `dfrtp_2_116380_244800` | 116.38 | 244.80 |
| column star counters | `col_cnt[6][1]` | `n672` | dfrtp | `dfrtp_2_114080_242080` | 114.08 | 242.08 |
| column star counters | `col_cnt[7][0]` | `n695` | dfrtp | `dfrtp_2_115920_255680` | 115.92 | 255.68 |
| column star counters | `col_cnt[7][1]` | `n693` | dfrtp | `dfrtp_2_113620_258400` | 113.62 | 258.40 |
| column star counters | `col_cnt[8][0]` | `n700` | dfrtp | `dfrtp_2_115920_280160` | 115.92 | 280.16 |
| column star counters | `col_cnt[8][1]` | `n704` | dfrtp | `dfrtp_2_113620_274720` | 113.62 | 274.72 |
| column star counters | `col_cnt[9][0]` | `n710` | dfrtp | `dfrtp_2_116380_277440` | 116.38 | 277.44 |
| column star counters | `col_cnt[9][1]` | `n706` | dfrtp | `dfrtp_2_114080_272000` | 114.08 | 272.00 |
| column star counters | `col_cnt[10][0]` | `n720` | dfrtp | `dfrtp_2_115920_282880` | 115.92 | 282.88 |
| column star counters | `col_cnt[10][1]` | `n721` | dfrtp | `dfrtp_2_113620_285600` | 113.62 | 285.60 |

# 口播广告摘除（2026-10-28）

双人节目里，中插口播常被声纹多切出一个声，再配进女声槽，听感像突然换人。这里只做英文口播句式，不做机器学习广告检测，也不重做已发布的 Cuban / Creatine。

准备阶段、翻译之前：

- 英文逐句（不分段合并之前）匹配口播句式。命中后，只在仍像口播、间隔不超过 6 秒、前后各最多 8 句或 90 秒的范围内扩成窗口。
- 窗口留在文稿里，`is_ad: true`，中文留空，不翻译、不朗读。合成时直接拼上前后对白，不补静音。
- 几乎整段都落在窗口里、而且很短（口播外不超过 20 秒，或全片占比不超过 15%）的说话人，折回邻近的真人，并记下 `folded_from`。折完之后人数仍须等于 `expected_speakers`。多出来的真人照旧挡住合成。`voices_confirmed` 不能绕过。
- 声纹工人仍按声明人数去聚类。结构检查（时间越界、无法对齐超过 20%）照旧。原始簇数不再拿来卡口播朗读人，否则广告段会在折声之前把整集挡掉。

默认声槽：1–3 人的新任务写 `prefer_male_slots: true`，自动分配只用 `male_1` / `male_2` / `male_3`。四人及以上仍用完整声槽，否则人数硬闸凑不齐不同的槽。两人节目里若确有女声，在确认框里改槽；把 `prefer_male_slots` 设为 false 只是放开完整声槽池，不会凭空把第二个人判成女声。

## 会摘的句式

不区分大小写。例如：brought to you by、this episode / show / podcast is sponsored、sponsored by、our sponsor(s)、sponsor message、word from our sponsor、thanks to our sponsor、paid partnership、use code、promo code、discount code、coupon code、ad break、we'll be right back、we will be right back、after the break、after this break、back after this、visit / head to / go to … slash 或 `/`。

窗口往外扩时，旁边的句子还要像口播（sponsor、promo、discount、percent off、free trial、link in the description、.com、www、slash、advertisement、limited time，以及上面的句式本身）。

## 摘不到的

- 只有音乐、静音或片头曲，口播没有说出来。
- 主人口播但完全不用这些句式（例如只说“我最近在用这款床垫”）。
- 中文口播。扫描的是英文原文。
- 句式前面那几句若没有口播用词，不会被扩进去。
- 真人嘉宾在广告窗外说话超过 20 秒，且占比超过 15%，会留下来；人数因此超出声明时，硬闸仍会挡住，不会偷偷合成。
- 正片里真的说到 “our sponsor” 这类词，可能被误摘。

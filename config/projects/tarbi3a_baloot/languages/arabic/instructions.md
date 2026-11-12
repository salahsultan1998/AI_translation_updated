LOCALIZATION PROFILE
Target locale: ar-SA.
Product: تربيعة, including Baloot, casual games, clubs, voice rooms, events, and the store.
Use clear, contemporary Arabic that feels familiar to Saudi players. Follow supplied context and relevant glossary entries. Do not assume every row belongs to Baloot or that a shared Chinese word has one Arabic meaning across all modules.

REGISTER BY TEXT TYPE
- Buttons, tabs, and short labels: concise, direct wording. Use clear action verbs for actions and nouns for section labels. Avoid unnecessary courtesy formulas.
- System messages and errors: calm, clear Arabic that states the problem and any action supplied by the source. Do not invent causes or solutions.
- Rules, tutorials, purchases, subscriptions, refunds, account deletion, and privacy text: precise, neutral Arabic. Preserve all conditions and consequences even when this requires a longer translation.
- Events and rewards: friendly and moderately lively. Preserve the original level of enthusiasm without adding urgency, superlatives, or promises.
- Preset social chat and character dialogue: natural Saudi expressions are appropriate when supported by the source and context. Do not force dialect into ordinary system text.
- When text type is unspecified: default to clear, lightly conversational standard Arabic.

BALOOT AND PRODUCT VOCABULARY
Use established Saudi Baloot terminology rather than literal dictionary translations. Distinguish game actions, projects, scoring, ranks, and rooms from similarly named features in other games.
For example, a Baloot buying or bidding action is not automatically a financial wager. Likewise, a Baloot rule challenge and a moderation report may require different wording.
Use contextual glossary distinctions for names such as صكة, الجلسات, سلم التحدي, عجلة الحظ, and عجلة السحب. Do not merge distinct features or turn a feature-specific mapping into a global replacement rule.

MANDATORY LEADERBOARD TERMINOLOGY
Translate 龙虎榜, 排行榜, 排名榜, 榜单, and any equivalent expression referring to an in-game leaderboard as المتصدرين or متصدرين, according to context. Use المتصدرين as the default standalone tab or screen title.
Never use قائمة المتصدرين, قائمة الترتيب, or any other wording that adds قائمة, القائمة, قوائم, or القوائم to a leaderboard reference. Do not substitute a generic board or table label for the approved term.
Use the approved terminology consistently in titles, buttons, descriptions, notifications, and compound expressions. Preserve meaningful qualifiers such as the event, period, region, or game mode, and rephrase surrounding text naturally to accommodate المتصدرين or متصدرين.
This is a mandatory product terminology override. Apply it even if a retrieved glossary entry or previous translation uses قائمة المتصدرين or another conflicting label.
Distinguish the leaderboard feature from an individual numerical rank: 排名 can still mean ترتيب or مركز when it refers to a player's position. The restriction on قائمة applies to leaderboard references, not unrelated lists.
Examples:
- 龙虎榜 → المتصدرين
- 排行榜 → المتصدرين
- 查看排行榜 → عرض المتصدرين
- 排行榜奖励 → مكافآت المتصدرين
Before returning the JSON, silently check every leaderboard reference for compliance with this rule.

ACCURATE, CULTURALLY APPROPRIATE WORDING
Use neutral, factual language for virtual currencies, play amounts, predictions, random rewards, and prizes. Apply a contextual product term only when it fits the described action.
For 下注, xz, 投资, and 许愿, apply the preferred feature terminology below. For related terms such as 投注, 竞猜, 赔率, and 奖金, likewise determine the actual meaning from the feature and applicable glossary. Preserve what the player spends, risks, receives, or can redeem.
Do not add gambling terminology to ordinary Baloot mechanics. Do not disguise an actual wager, payment, loss risk, or cash-out mechanism as a free gift, purchase, donation, or harmless prediction.
Keep virtual currency separate from real money. Never infer that coins can or cannot be withdrawn, sold, transferred, refunded, or exchanged for cash.
Preserve uncertainty: “a chance to win” must not become a guaranteed reward. Do not add “free,” “guaranteed,” “risk-free,” “cash,” or regulatory approval unless explicitly supported.
Localize humor and social expressions without adding insults, stereotypes, religious expressions, sexual innuendo, or culturally sensitive imagery absent from the source. Preserve player-facing moderation reasons and disclosures accurately.

PREFERRED FEATURE TERMINOLOGY: 下注 / xz / 投资 / 许愿
Prefer ordinary, natural Arabic feature labels such as دعم, توقع, مشاركة, and اختيار when they accurately describe the player's action. Do not default to رهان, مراهنة, مقامرة, or استثمار مالي merely because the Chinese wording or internal shorthand suggests one of those concepts.
- 下注 / xz: use دعم for a feature where the player supports a participant or option; use توقع for a feature where the player selects an expected outcome. Treat xz as an alias of 下注 only when the supplied feature context or glossary establishes that usage. Never translate xz inside a key, placeholder, URL, or other protected identifier.
- 投资: use دعم when it means contributing to or supporting an in-game participant, option, or event. Do not infer a financial investment from the word alone. If the source actually describes a financial investment, retain that meaning and its conditions.
- 许愿: use اختيار المكافأة when the feature lets the player select a desired reward; use توقع only when the action actually predicts an outcome. For an actual wish or wish list, use أمنية or أمنيات as appropriate. Do not turn wishing into support or prediction without contextual evidence.
Use the approved term consistently within the same feature. Choose the grammatical form required by the UI: دعم / ادعم for support, توقع / توقّع for prediction, and اختيار / اختر for selection. Do not alternate synonyms for the same action.
Translate compounds using the same contextual term. For example, in a support feature, 下注金额 can be قيمة الدعم and 下注成功 can be تم تسجيل دعمك. In a prediction feature, 下注成功 can be تم تسجيل توقعك. These are context-specific examples, not global replacement rules.
These naming preferences must not remove or change participation costs, deductions, probabilities, possible losses, payout conditions, or withdrawal terms. A virtual currency alone does not establish that an action is free or risk-free. Use precise wording wherever a neutral label would otherwise mislead the player about the actual mechanism.
If the supplied context and glossary do not distinguish support, prediction, reward selection, or wishing, do not invent a mechanism just to use a preferred label. Follow the system prompt's ambiguity rule.

ARABIC AND UI QUALITY
Prioritize meaning and clarity, then brevity. Produce the shortest natural wording that preserves the full message. Do not omit conditions or protected tokens to fit a length target.
When max_chars is supplied, try to fit it by rephrasing. If it cannot be met faithfully, preserve the complete meaning for the application's length checker to flag. Do not claim the limit was satisfied.
Use correct spelling, agreement, and punctuation. Avoid unnecessary diacritics, ornamental elongation, and repeated exclamation marks.
Address the player directly where appropriate. Do not infer a person's gender from a username or placeholder. Use wording that avoids unnecessary gender assumptions.
When a numeric variable's value is unknown, use a natural construction that does not assume singular, dual, or a specific plural category. Do not invent runtime plural syntax, extra placeholders, or slash-separated alternatives.
Use 0-9 for newly written Arabic UI numerals unless a supplied specification says otherwise. Preserve IDs, version strings, protected numbers, numerical values, and precision. Convert Chinese magnitude notation accurately when necessary: 10万 means 100000, not 10. Never convert currencies, calendars, or time zones without an explicit instruction and the required conversion information.
Retain approved technical labels such as VIP, SVIP, ID, and Wi-Fi when appropriate. Translate ordinary Chinese or English prose into Arabic.

ILLUSTRATIVE EXAMPLES
These demonstrate behavior; explicit mandatory terminology rules take precedence, followed by supplied applicable glossary entries.
- Source: 排行榜 / 龙虎榜 | Context: leaderboard title | Arabic: المتصدرين
- Source: 金币不足 | Glossary: 金币 → ذهب | Arabic: الذهب غير كافٍ
- Source: 获得{1}金币 | Context: reward received notification | Glossary: 金币 → ذهب | Arabic: حصلت على {1} ذهب
- Source: 有机会获得大奖 | Arabic: فرصة للفوز بالجائزة الكبرى
- Source: 删除账号后无法恢复 | Arabic: لا يمكن استعادة الحساب بعد حذفه
- Source: <b>获得{1}金币</b> | Context: reward received notification | Glossary: 金币 → ذهب | Arabic: <b>حصلت على {1} ذهب</b>

Return only the JSON required by the system prompt.
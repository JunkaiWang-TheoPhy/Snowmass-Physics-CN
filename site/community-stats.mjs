export const FRONTIERS = [
  ["AF", "加速器前沿", "Accelerator Frontier"],
  ["CEF", "社群参与前沿", "Community Engagement Frontier"],
  ["CompF", "计算前沿", "Computational Frontier"],
  ["CF", "宇宙前沿", "Cosmic Frontier"],
  ["EF", "能量前沿", "Energy Frontier"],
  ["IF", "仪器学前沿", "Instrumentation Frontier"],
  ["NF", "中微子前沿", "Neutrinos Frontier"],
  ["RPF", "稀有过程与精密测量前沿", "Rare Processes & Precision Measurements Frontier"],
  ["TF", "理论前沿", "Theory Frontier"],
  ["UF", "地下设施与基础设施前沿", "Underground Facilities & Infrastructure Frontier"],
];

const TRANSLATION_STATES = ["not-started", "machine-draft", "human-review", "published"];
const AUTHORIZATION_STATES = ["license-cleared", "needs-permission", "contacted", "response-pending", "permission-granted", "permission-denied"];

function emptyCounts(keys) {
  return Object.fromEntries([...keys, "other"].map((key) => [key, 0]));
}

export function summarizePapers(papers) {
  const records = Array.isArray(papers) ? papers : [];
  const translation = emptyCounts(TRANSLATION_STATES);
  const authorization = emptyCounts(AUTHORIZATION_STATES);
  const frontiers = Object.fromEntries(FRONTIERS.map(([code]) => [code, {
    total: 0, allowed: 0, blocked: 0,
    "machine-draft": 0, "human-review": 0, published: 0,
  }]));
  let allowed = 0;

  for (const paper of records) {
    const translationKey = TRANSLATION_STATES.includes(paper?.translation_status) ? paper.translation_status : "other";
    const authorizationKey = AUTHORIZATION_STATES.includes(paper?.authorization_status) ? paper.authorization_status : "other";
    const isAllowed = paper?.publication_allowed === true;
    translation[translationKey] += 1;
    authorization[authorizationKey] += 1;
    if (isAllowed) allowed += 1;
    const uniqueFrontiers = new Set(Array.isArray(paper?.frontiers) ? paper.frontiers : []);
    for (const code of uniqueFrontiers) {
      if (!frontiers[code]) continue;
      frontiers[code].total += 1;
      frontiers[code][isAllowed ? "allowed" : "blocked"] += 1;
      if (["machine-draft", "human-review", "published"].includes(translationKey)) {
        frontiers[code][translationKey] += 1;
      }
    }
  }

  return {
    total: records.length,
    publication: { allowed, blocked: records.length - allowed },
    translation,
    authorization,
    frontiers,
  };
}

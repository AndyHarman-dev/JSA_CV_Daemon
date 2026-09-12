"""Structured CV schema — tolerant by design.

A CVDocument is a contact block plus an ordered list of sections. Each section is a
single **uniform** shape (a name + optional free text + optional flat items + optional
sub-entries) rather than a discriminated union of rigid typed sections.

Why uniform/tolerant instead of a tagged union:
    A live run showed the model reliably produces *good CV content* but will not reliably
    emit a machine-oriented tagged union. It dropped the ``type`` discriminator on every
    section (→ ``union_tag_not_found``), used ``title`` and ``name`` interchangeably, and
    invented content keys (``content``/``items``/``subsections``) — and held those shapes
    through two self-heal corrections. The model has a strong, sensible prior: a CV section
    is "a named block with content." So the schema meets that prior instead of fighting it.

Robustness model:
    - ``extra="ignore"`` (not ``forbid``): stray/invented keys are dropped, not fatal.
    - A ``mode="before"`` normalizer classifies content **by Python type**, not by exact
      key name — a list of strings becomes ``items``, a list of objects becomes ``entries``,
      a string becomes ``text`` — so whatever key the model picks from a generous-but-bounded
      set lands in the right place.

Contamination defense — what the schema gate covers, and where it stops:
    Stray keys (change-log, commentary) can't leak because the **serializer only renders
    known fields**, and the CV/cover-letter use **separate per-stage schemas** so they
    can't share a payload. But separate schemas only stop the two stages *sharing* a
    payload — they do NOT stop the model from emitting the wrong *content-kind* into the
    right *shape*. A live run proved this: at the cv_adjust stage the model spontaneously
    wrote a cover letter (one nameless section of prose paragraphs), which satisfied
    "contact + ≥1 section" and shipped a letter labelled "CV". So a **content-kind guard**
    (`_not_a_cover_letter`) is a hard gate here: letter formulas in the CV text reject the
    payload (→ self-heal → fail if uncorrected). A letter-as-CV is *corruption*, not
    thinness, so failing the job is the right outcome.

    The hard gates are therefore: contact ``name`` + ≥1 renderable section + not-a-letter.
    Everything else stays permissive — a false reject kills a job, a lenient accept only
    renders a slightly-thin CV the user still reviews. (A *missing summary* is thinness, not
    corruption, so it is NOT gated here — it is nudged in the self-heal loop and tolerated
    if uncorrected; see jsa/pipeline/stages.py::_self_heal_final.)
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


class _Loose(BaseModel):
    # Tolerant base: ignore unknown keys (no contamination risk — the serializer renders
    # only known fields) and accept population by field name.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --- helpers ---------------------------------------------------------------------------

# A section's content can arrive under any key the model invents. Rather than an
# allowlist of content keys (which silently *drops* content under an unanticipated key —
# an invisible CV-thinning bug), we use a denylist of "meta" keys and absorb everything
# else, classifying by value *type* (str→text, list[str]→items, list[dict]→entries).
# Over-absorbing a stray label is a visible, rare nuisance; silently losing real content
# is invisible and reintroduces the content-fidelity problem this whole change fixes.
_SECTION_META_KEYS = frozenset({
    "name", "title", "heading", "section", "section_name", "section_title",
    "label", "type", "kind", "id", "order", "index", "icon",
})

_HEADING_KEYS = ("heading", "title", "role", "position", "name", "degree", "project", "label")
_SUBHEADING_KEYS = ("subheading", "subtitle", "company", "organization", "employer",
                    "institution", "school", "issuer")
_DATE_KEYS = ("dates", "date", "period", "duration", "when", "years", "year")
_LOCATION_KEYS = ("location", "place", "city")
_TEXT_KEYS = ("text", "description", "summary", "content", "detail")
_BULLET_KEYS = ("bullets", "points", "highlights", "achievements", "responsibilities",
                "items", "details", "tasks", "list")
_NAME_KEYS = ("name", "title", "heading", "section", "section_name", "label")
# Discriminator-style keys some models emit instead of a name (e.g. {"type": "summary"}).
# Fallback only — an explicit name/title key always wins (see Section._classify).
_NAME_FALLBACK_KEYS = ("type", "kind")

# Entry-level meta keys to skip when absorbing leftover content (the entry analogue of
# _SECTION_META_KEYS). Everything not consumed as a known field and not meta is absorbed —
# URLs → links, other strings → text, lists → bullets — so a `url`/`stack`/`technologies`
# key the model invents is never silently dropped.
_ENTRY_META_KEYS = frozenset({"type", "kind", "id", "order", "index", "icon"})

# Matches an explicit link key or a URL-shaped string (github.com/..., https://..., www…).
_LINK_KEYS = frozenset({"url", "link", "links", "repo", "repository", "github",
                        "gitlab", "href", "website", "homepage", "demo", "live"})
_URL_RE = re.compile(r"(https?://|www\.|[\w-]+\.(?:com|org|io|dev|net|app|gg|me|co|ai)/\S)", re.I)

# Cover-letter "tells": structural formulas that belong in a letter, never a CV. Used by the
# content-kind guard to reject a payload where the model wrote a cover letter into the CV
# shape (an observed live failure). Deliberately excludes sentiment words ("passionate
# about") that legitimately appear in CV summaries — only formulaic letter idioms. A single
# accidental match is tolerated; two or more is decisive (the observed failure matched three).
#
# Per-language catalog: once the pipeline can emit non-English CV/cover-letter output, an
# English-only guard goes blind to a non-English letter mis-emitted as a CV. Each language's
# tuple mirrors the same ~12 formulaic idioms (translated, not reinvented). All alternatives
# use non-capturing groups (`(?:...)`) — `re.findall` returns *group contents* when a pattern
# has capturing groups, which would silently corrupt both the match count and the sample
# message built from the hits.
_LETTER_FORMULAS_BY_LANG: dict[str, tuple[str, ...]] = {
    "en": (
        r"writing to express",
        r"express my (?:strong |sincere |keen )?interest",
        r"would welcome (?:the opportunity|discussing|the chance)",
        r"ready to contribute immediately",
        r"\bdear hiring\b",
        r"\bdear (?:sir|madam|mr|ms|mrs)\b",
        r"\bsincerely,",
        r"\byours (?:sincerely|faithfully|truly)\b",
        r"thank you for (?:your )?consider",
        r"look forward to (?:hearing|discussing|speaking)",
        r"\bi am (?:writing|applying) (?:to|for)\b",
        r"\bi'?m applying for\b",
    ),
    "es": (
        r"le escribo para expresar",
        r"expresar mi (?:gran |sincero |vivo )?inter[ée]s",
        r"me encantar[ií]a (?:la oportunidad|conversar|la posibilidad)",
        r"list[oa] para contribuir de inmediato",
        r"\bestimado responsable de contrataci[oó]n\b",
        r"\bestimados? (?:se[nñ]or|se[nñ]ora)\b",
        r"\batentamente,",
        r"\b(?:un cordial saludo|saludos cordiales)\b",
        r"gracias por (?:su )?consideraci[oó]n",
        r"espero (?:tener noticias|conversar|poder hablar)",
        r"\ble escribo para (?:solicitar|postular)\b",
        r"\bestoy postulando (?:a|para)\b",
    ),
    "fr": (
        r"je vous [ée]cris pour exprimer",
        r"exprimer mon (?:vif |sinc[èe]re |profond )?int[ée]r[êe]t",
        r"serais ravi(?:e)? de (?:l'opportunit[ée]|en discuter|l'occasion)",
        r"pr[êe]t(?:e)? [àa] contribuer imm[ée]diatement",
        r"\bmadame,? monsieur\b",
        r"\bcher (?:monsieur|madame)\b",
        r"\bcordialement,",
        r"\bveuillez agr[ée]er\b",
        r"vous remercie de (?:l'attention|votre consid[ée]ration)",
        r"dans l'attente de (?:votre r[ée]ponse|[ée]changer|vous rencontrer)",
        r"\bje vous [ée]cris pour (?:postuler|solliciter)\b",
        r"\bje postule (?:au poste|pour le poste)\b",
    ),
    "de": (
        r"schreibe ihnen,? um mein interesse",
        r"mein (?:gro[ßs]es |aufrichtiges |starkes )?interesse zu bekunden",
        r"w[üu]rde mich (?:sehr )?freuen,? (?:die gelegenheit|dar[üu]ber zu sprechen)",
        r"bereit,? sofort einen beitrag zu leisten",
        r"\bsehr geehrte damen und herren\b",
        r"\bsehr geehrte[rn]? (?:herr|frau)\b",
        r"\bmit freundlichen gr[üu][sß]en\b",
        r"\bhochachtungsvoll\b",
        r"danke (?:ihnen )?f[üu]r (?:ihre )?ber[üu]cksichtigung",
        r"freue mich (?:auf ein gespr[äa]ch|darauf,? von ihnen zu h[öo]ren)",
        r"\bich bewerbe mich (?:hiermit )?(?:auf|f[üu]r)\b",
        r"\bhiermit bewerbe ich mich\b",
    ),
    "pt": (
        r"escrevo para expressar",
        r"expressar meu (?:forte |sincero |grande )?interesse",
        r"adoraria (?:a oportunidade|discutir|conversar)",
        r"pronto[a]? para contribuir imediatamente",
        r"\bprezad[oa] respons[áa]vel pela contrata[çc][ãa]o\b",
        r"\bprezad[oa] (?:senhor|senhora)\b",
        r"\batenciosamente,",
        r"\bcordialmente\b",
        r"obrigad[oa] pela (?:sua )?considera[çc][ãa]o",
        r"aguardo (?:retorno|a oportunidade de conversar|not[íi]cias)",
        r"\bescrevo para (?:candidatar-me|me candidatar)\b",
        r"\bestou me candidatando (?:a|para)\b",
    ),
    "it": (
        r"scrivo per esprimere",
        r"esprimere il mio (?:forte |sincero |vivo )?interesse",
        r"sarei felice di (?:discuterne|un'opportunit[àa]|parlarne)",
        r"pronto[a]? a contribuire immediatamente",
        r"\bgentile responsabile delle assunzioni\b",
        r"\begregio (?:signore|signora)\b",
        r"\bcordiali saluti,",
        r"\bdistinti saluti\b",
        r"grazie per la (?:vostra |sua )?considerazione",
        r"resto in attesa di (?:un riscontro|discuterne|sue notizie)",
        r"\bscrivo per candidarmi\b",
        r"\bmi candido per (?:la posizione|il ruolo)\b",
    ),
    "nl": (
        r"schrijf u (?:hierbij )?om mijn interesse",
        r"interesse (?:oprecht |sterk |bijzonder )?(?:kenbaar te maken|te uiten)",
        r"zou de (?:kans|gelegenheid|mogelijkheid) (?:graag )?(?:grijpen|bespreken)",
        r"klaar om (?:onmiddellijk|direct) (?:bij te dragen|van start te gaan)",
        r"\bgeachte (?:heer|mevrouw)\b",
        r"\bgeachte (?:heer\/mevrouw|sollicitatiecommissie)\b",
        r"\bmet vriendelijke groet,",
        r"\bhoogachtend\b",
        r"dank (?:u |je )?voor (?:uw|je) overweging",
        r"(?:kijk|zie) uit naar (?:uw reactie|een gesprek|het vervolg)",
        r"\bik schrijf (?:u |je )?om te solliciteren\b",
        r"\bik solliciteer (?:naar|voor) (?:de|deze) (?:functie|positie|vacature)\b",
    ),
    "sv": (
        r"skriver (?:till er )?f[öo]r att uttrycka",
        r"uttrycka mitt (?:starka |uppriktiga |stora )?intresse",
        r"skulle (?:g[äa]rna|varmt) (?:v[äa]lkomna m[öo]jligheten|diskutera|h[öo]ras)",
        r"redo att bidra omedelbart",
        r"\bb[äa]sta (?:rekryterare|hr-ansvarig)\b",
        r"\bb[äa]ste herr\b|\bk[äa]ra (?:herr|fru)\b",
        r"\bmed v[äa]nliga h[äa]lsningar,",
        r"\bh[öo]gaktningsfullt\b",
        r"tack f[öo]r (?:ert|ditt) [öo]vervägande",
        r"ser fram emot (?:att h[öo]ra|en intervju|att diskutera)",
        r"\bjag skriver f[öo]r att s[öo]ka\b",
        r"\bjag s[öo]ker (?:tj[äa]nsten|befattningen)\b",
    ),
    "pl": (
        r"pisz[eę] (?:do pa[nń]stwa )?aby wyrazi[cć]",
        r"wyrazi[cć] (?:moje |swoje )?(?:silne |szczere )?zainteresowanie",
        r"z (?:rado[śs]ci[aą] )?(?:om[oó]wi[eę]|przyjm[eę] mo[żz]liwo[śs][cć])",
        r"gotow[ay] do (?:natychmiastowego )?wk[łl]adu",
        r"\bszanowni pa[nń]stwo\b",
        r"\bszanown[ay] pan[iie]?\b",
        r"\bz powa[żz]aniem,",
        r"\bz wyrazami szacunku\b",
        r"dzi[eę]kuj[eę] za (?:pa[nń]stwa )?rozwa[żz]enie",
        r"z niecierpliwo[śs]ci[aą] oczekuj[eę] (?:na rozmow[eę]|odpowiedzi)",
        r"\bpisz[eę], aby (?:aplikowa[cć]|z[łl]o[żz]y[cć] podanie)\b",
        r"\baplikuj[eę] na stanowisko\b",
    ),
    "ru": (
        r"пишу,? чтобы выразить",
        r"выразить (?:свой |искренний |большой )?интерес",
        r"был(?:а)? бы рад(?:а)? (?:возможности|обсудить)",
        r"готов(?:а)? внести вклад немедленно",
        r"\bуважаемый (?:менеджер по подбору персонала|специалист по кадрам)\b",
        r"\bуважаем(?:ый|ая) (?:господин|госпожа)\b",
        r"\bс уважением,",
        r"\bискренне ваш(?:а)?\b",
        r"благодарю за (?:ваше )?рассмотрение",
        r"с нетерпением жду (?:ответа|встречи|обсуждения)",
        r"\bпишу,? чтобы податься на\b",
        r"\bподаю заявку на (?:позицию|должность)\b",
    ),
    "tr": (
        r"ilgimi (?:belirtmek|ifade etmek) i[cç]in yaz[ıi]yorum",
        r"(?:g[üu][cç]l[üu] |i[cç]ten )?ilgimi (?:belirtmek|ifade etmek)",
        r"f[ıi]rsat[ıi] (?:memnuniyetle )?(?:kar[şs][ıi]lar[ıi]m|g[öo]r[üu][şs]meyi isterim)",
        r"hemen katk[ıi]da bulunmaya haz[ıi]r[ıi]m",
        r"\bsay[ıi]n insan kaynaklar[ıi]\b",
        r"\bsay[ıi]n (?:bay|bayan)\b",
        r"\bsayg[ıi]lar[ıi]mla,",
        r"\bsayg[ıi]lar[ıi]m[ıi]zla\b",
        r"de[ğg]erlendirmeniz i[çc]in te[şs]ekk[üu]r ederim",
        r"(?:yan[ıi]t[ıi]n[ıi]z[ıi]|g[öo]r[üu][şs]meyi) sab[ıi]rs[ıi]zl[ıi]kla bekliyorum",
        r"\bba[şs]vurmak i[çc]in yaz[ıi]yorum\b",
        r"\b(?:pozisyona|g[öo]reve) ba[şs]vuruyorum\b",
    ),
    "ar": (
        r"أكتب (?:إليكم )?للتعبير",
        r"(?:التعبير عن|أعبر عن) اهتمامي",
        r"يسعدني (?:الفرصة|مناقشة|التحدث)",
        r"مستعد(?:ة)? للمساهمة فورا",
        r"عزيزي مسؤول التوظيف",
        r"السيد(?:ة)? العزيز(?:ة)?",
        r"مع خالص التقدير",
        r"وتفضلوا بقبول فائق الاحترام",
        r"شكرا لكم على النظر",
        r"أتطلع (?:إلى سماع ردكم|لمناقشة|للحديث)",
        r"أكتب (?:إليكم )?للتقدم",
        r"أتقدم (?:بطلب|لوظيفة)",
    ),
    "he": (
        r"אני כותב(?:ת)? כדי להביע",
        r"להביע את (?:התעניינותי|עניין רב)",
        r"אשמח (?:להזדמנות|לדון|לשוחח)",
        r"מוכן(?:ה)? לתרום מיד",
        r"לכבוד מנהל(?:ת)? הגיוס",
        r"אדון\/גברת נכבד(?:ה)?",
        r"בברכה,",
        r"בכבוד רב",
        r"תודה על (?:התייחסותכם|שיקולכם)",
        r"מצפה (?:לשמוע|לדון|לשוחח)",
        r"אני כותב(?:ת)? כדי להגיש מועמדות",
        r"אני מגיש(?:ה)? מועמדות ל",
    ),
    "hi": (
        r"अपनी रुचि (?:व्यक्त|प्रकट) करने के लिए लिख रह[ाी] ह",
        r"(?:गहरी|प्रबल|ईमानदार) रुचि व्यक्त कर",
        r"अवसर का स्वागत कर[ूें]ग[ाी]|चर्चा करना चाहत[ाी]",
        r"तुरंत योगदान देने के लिए तैयार",
        r"प्रिय नियुक्ति प्रबंधक",
        r"प्रिय महोदय\/महोदया",
        r"सादर,",
        r"सधन्यवाद",
        r"आपके विचार के लिए धन्यवाद",
        r"(?:सुनने|चर्चा करने|बातचीत करने) की प्रतीक्षा",
        r"मैं आवेदन करने के लिए लिख रह[ाी]",
        r"मैं (?:पद|नौकरी) के लिए आवेदन कर रह[ाी]",
    ),
    "zh": (
        r"谨此表达",
        r"表达(?:我|本人)(?:浓厚|真诚|强烈)?(?:的)?兴趣",
        r"期待(?:有机会|讨论|沟通)",
        r"随时准备立即贡献",
        r"尊敬的招聘经理",
        r"尊敬的(?:先生|女士)",
        r"此致\s*敬礼",
        r"顺颂商祺",
        r"感谢您(?:的)?考虑",
        r"期待(?:您的回复|进一步沟通|面谈)",
        r"特此申请",
        r"我谨申请(?:该职位|此职位)",
    ),
    "ja": (
        r"貴社に(?:応募|志望)させていただきたく",
        r"強い関心を(?:持って|抱いて)おります",
        r"(?:機会をいただければ|お話しできれば)幸いです",
        r"即戦力として貢献する準備ができております",
        r"採用ご担当者様",
        r"拝啓",
        r"敬具",
        r"よろしくお願い申し上げます",
        r"ご検討いただき(?:ありがとうございます|感謝いたします)",
        r"お返事(?:を)?お待ちしております",
        r"応募させていただきます",
        r"この度.{0,20}応募いたします",
    ),
    "ko": (
        r"관심을 표(?:현|명)하고자 (?:이 글을 )?씁니다",
        r"(?:깊은|강한|진심 어린) 관심을 표(?:현|명)",
        r"(?:기회를 주시면|논의할 수 있다면) 감사하겠습니다",
        r"즉시 기여할 준비가 되어 있습니다",
        r"채용 담당자님께",
        r"친애하는 (?:귀하|담당자)",
        r"경구|올림",
        r"고려해 주셔서 감사합니다",
        r"(?:답변을|면접을|논의를) 기다리겠습니다",
        r"지원하고자 (?:이 글을 )?씁니다",
        r"(?:직책|포지션)에 지원합니다",
    ),
    "vi": (
        r"viết (?:thư này )?để bày tỏ",
        r"bày tỏ (?:sự quan tâm|niềm đam mê) (?:sâu sắc|chân thành)?",
        r"rất mong (?:có cơ hội|được thảo luận|trao đổi)",
        r"sẵn sàng đóng góp ngay lập tức",
        r"\bkính gửi (?:nhà tuyển dụng|bộ phận nhân sự)\b",
        r"\bkính thưa (?:ông|bà)\b",
        r"\btrân trọng,",
        r"\bkính thư\b",
        r"cảm ơn (?:quý công ty|anh\/chị) đã xem xét",
        r"mong (?:sớm nhận được phản hồi|được trao đổi thêm)",
        r"viết thư này để ứng tuyển",
        r"tôi xin ứng tuyển (?:vào vị trí|cho vị trí)",
    ),
    "th": (
        r"เขียนจดหมายฉบับนี้เพื่อแสดง",
        r"แสดงความสนใจ(?:อย่างยิ่ง|อย่างแท้จริง)?",
        r"ยินดีเป็นอย่างยิ่ง(?:หากมีโอกาส|ที่จะได้พูดคุย)",
        r"พร้อมที่จะร่วมงานได้ทันที",
        r"เรียน ฝ่ายทรัพยากรบุคคล",
        r"เรียน คุณ",
        r"ขอแสดงความนับถือ",
        r"ด้วยความเคารพอย่างสูง",
        r"ขอขอบคุณที่ให้ความพิจารณา",
        r"หวังว่าจะได้รับการติดต่อกลับ",
        r"เขียนจดหมายฉบับนี้เพื่อสมัคร",
        r"ขอสมัครตำแหน่ง",
    ),
    "id": (
        r"menulis (?:surat ini )?untuk menyatakan",
        r"menyatakan minat (?:saya )?(?:yang besar|yang tulus)?",
        r"akan (?:sangat )?senang (?:menyambut kesempatan|mendiskusikan)",
        r"siap berkontribusi segera",
        r"\byang terhormat (?:manajer perekrutan|hrd)\b",
        r"\byang terhormat (?:bapak|ibu)\b",
        r"\bhormat saya,",
        r"\bsalam hormat\b",
        r"terima kasih atas pertimbangan(?:nya)?",
        r"menantikan (?:kabar|kesempatan berdiskusi|wawancara)",
        r"menulis surat ini untuk melamar",
        r"saya mengajukan lamaran untuk posisi",
    ),
}

_LETTER_FORMULA_RE_BY_LANG: dict[str, re.Pattern] = {
    lang: re.compile("|".join(formulas), re.I)
    for lang, formulas in _LETTER_FORMULAS_BY_LANG.items()
}

_LETTER_MATCH_THRESHOLD = 2


def _letter_formula_re(language: str) -> re.Pattern:
    """The compiled letter-formula pattern for ``language``.

    Falls back to the English pattern for any language not yet in the catalog — a
    partial guard (still catching literal English letter idioms) beats no guard at all.
    """
    return _LETTER_FORMULA_RE_BY_LANG.get(language, _LETTER_FORMULA_RE_BY_LANG["en"])

# Names that mark a summary/profile section. Used by the (soft) summary nudge in the
# self-heal loop via cv_has_summary() — NOT a hard schema gate (a missing summary is
# thinness, not corruption).
SUMMARY_NAME_RE = re.compile(r"\b(summary|profile|objective|about|overview)\b", re.I)


def _first_str(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string value among ``keys`` (in order)."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _str_list(value: Any) -> list[str]:
    """Coerce a value into a list of non-empty strings (drops non-string members)."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [x.strip() for x in value if isinstance(x, str) and x.strip()]
    return []


# --- models ----------------------------------------------------------------------------


class Contact(_Loose):
    """Candidate identity + contact channels. Only ``name`` is required."""

    name: str = Field(validation_alias="name")
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)  # linkedin / github / portfolio URLs

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        out: dict[str, Any] = {}
        name = _first_str(d, ("name", "full_name", "fullName", "fullname"))
        if name is not None:
            out["name"] = name
        for canonical, keys in (
            ("email", ("email", "e-mail", "mail")),
            ("phone", ("phone", "telephone", "tel", "mobile")),
            ("location", ("location", "address", "city")),
        ):
            v = _first_str(d, keys)
            if v is not None:
                out[canonical] = v
        links: list[str] = []
        for k in ("links", "urls", "websites", "profiles", "social"):
            links.extend(_str_list(d.get(k)))
        # Common single-link fields.
        for k in ("linkedin", "github", "portfolio", "website", "url"):
            links.extend(_str_list(d.get(k)))
        if links:
            out["links"] = links
        return out

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("contact.name must be non-empty")
        return v.strip()


class Entry(_Loose):
    """A uniform sub-entry: one job, degree, project, certification, etc.

    All fields optional. The serializer renders whichever are present:
    ``**heading** — subheading`` / ``*dates | location*`` / text paragraph / ``- bullets``.
    """

    heading: str | None = None       # role / project / degree / award title
    subheading: str | None = None    # company / institution / issuer
    dates: str | None = None
    location: str | None = None
    text: str | None = None          # a prose description for the entry
    bullets: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)  # repo / demo / portfolio URLs

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, str):  # a bare string entry → its text
            return {"text": data.strip()} if data.strip() else {}
        if not isinstance(data, dict):
            return data
        d = dict(data)
        out: dict[str, Any] = {}
        used: set[str] = set(k for k in d if k in _ENTRY_META_KEYS)
        for canonical, keys in (
            ("heading", _HEADING_KEYS),
            ("subheading", _SUBHEADING_KEYS),
            ("dates", _DATE_KEYS),
            ("location", _LOCATION_KEYS),
            ("text", _TEXT_KEYS),
        ):
            for k in keys:
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    out[canonical] = v.strip()
                    used.add(k)
                    break
        # Absorb every leftover value so nothing is silently dropped (the bug that lost
        # per-project `url` keys): URL-shaped strings → links, other strings → extra text,
        # lists → bullets (with URL members split off into links).
        bullets: list[str] = []
        links: list[str] = []
        extra_text: list[str] = []
        for k, v in d.items():
            if k in used:
                continue
            if isinstance(v, list):
                for x in _str_list(v):
                    (links if _URL_RE.search(x) else bullets).append(x)
            elif isinstance(v, str) and v.strip():
                s = v.strip()
                if k in _LINK_KEYS or _URL_RE.search(s):
                    links.append(s)
                else:
                    extra_text.append(s)
        if extra_text:
            out["text"] = "\n\n".join(([out["text"]] if out.get("text") else []) + extra_text)
        if bullets:
            out["bullets"] = bullets
        if links:
            out["links"] = links
        return out

    # No "must have content" guard: with denylist absorption a stray object (e.g. a
    # `metadata: {…}` key) can normalize to an empty entry. Failing validation there would
    # false-reject the whole CV; instead an empty entry is harmless — the serializer renders
    # nothing for it (see jsa/render/serialize.py::_entry_block).


class Section(_Loose):
    """A uniform CV section: a name plus content in any of three shapes.

    Content is classified by type: free-text prose (``text``), a flat keyword/skill list
    (``items``), and/or structured sub-entries (``entries``). A section may populate more
    than one (e.g. an intro paragraph followed by entries).
    """

    name: str = ""
    text: str | None = None
    items: list[str] = Field(default_factory=list)
    entries: list[Entry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _classify(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        explicit_name = _first_str(d, _NAME_KEYS)
        name = explicit_name or _first_str(d, _NAME_FALLBACK_KEYS) or ""
        if not explicit_name and name:
            # A discriminator value ("summary", "work_experience") isn't a heading —
            # title-case it for rendering, same as an author-written heading would be.
            name = name.replace("_", " ").replace("-", " ").title()
        text_parts: list[str] = []
        items: list[str] = []
        entries: list[Any] = []
        for key, val in d.items():
            if key in _SECTION_META_KEYS:
                continue
            if isinstance(val, str):
                if val.strip():
                    text_parts.append(val.strip())
            elif isinstance(val, dict):
                entries.append(val)
            elif isinstance(val, list):
                if any(isinstance(x, dict) for x in val):
                    entries.extend(x for x in val if isinstance(x, dict))
                    # stray strings in a mixed list become items
                    items.extend(x.strip() for x in val if isinstance(x, str) and x.strip())
                else:
                    items.extend(_str_list(val))
        out: dict[str, Any] = {"name": name}
        if text_parts:
            out["text"] = "\n\n".join(text_parts)
        if items:
            out["items"] = items
        if entries:
            out["entries"] = entries
        return out


# How many content-free sections a CV may carry before it is read as a skeleton rather
# than a CV with one stray section. 1 preserves the long-standing "an individual empty/odd
# section is tolerated" behaviour exactly; 2+ is the observed structured-output failure
# (see CVDocument._has_renderable_content).
_MAX_EMPTY_SECTIONS = 1


def _section_has_content(s: Section) -> bool:
    if s.text or s.items:
        return True
    return any(
        (e.heading or e.subheading or e.text or e.bullets or e.links) for e in s.entries
    )


def _cv_text_blob(sections: list[Section]) -> str:
    """All human-readable strings in the CV, for content-kind heuristics."""
    parts: list[str] = []
    for s in sections:
        if s.name:
            parts.append(s.name)
        if s.text:
            parts.append(s.text)
        parts.extend(s.items)
        for e in s.entries:
            for v in (e.heading, e.subheading, e.text):
                if v:
                    parts.append(v)
            parts.extend(e.bullets)
    return "\n".join(parts)


def cv_has_summary(cv: "CVDocument") -> bool:
    """True if the CV carries a summary/profile section with prose or items.

    Used by the self-heal loop for a *soft* nudge (see jsa/pipeline/stages.py); a missing
    summary is deliberately NOT a hard validation failure.
    """
    return any(
        SUMMARY_NAME_RE.search(s.name or "") and (s.text or s.items) for s in cv.sections
    )


class CVDocument(_Loose):
    """A complete, structured CV. The deliverable of the cv_adjust stage."""

    contact: Contact
    sections: list[Section] = Field(min_length=1)

    @model_validator(mode="after")
    def _has_renderable_content(self) -> "CVDocument":
        # The CV-vs-noise gate lives here, at the document level: contact.name (required on
        # Contact) plus sections that actually render something. An individual empty/odd
        # section is still tolerated (the serializer skips it) so this can't false-reject an
        # otherwise-good CV — but a payload with no renderable content at all is rejected,
        # and so is a SKELETON: several sections present by name with nothing in them.
        #
        # The skeleton arm is the structural half of the schema-minimums fix (the prompt
        # half is `jsa/pipeline/prompt_assembly.py::_completeness_clause`). Confirmed live
        # on gemini-3.5-flash: a cv_adjust FINAL came back as Summary + three sections with
        # `text: null`, which satisfied the old "at least one section has content" test and
        # was rendered to PDF/DOCX as a one-paragraph "CV". Nothing else in the pipeline
        # would have caught it — this validator is the only gate before the DB write.
        empty = [s.name or "(unnamed)" for s in self.sections if not _section_has_content(s)]
        if len(empty) == len(self.sections):
            raise ValueError("CV has no renderable section content")
        if len(empty) > _MAX_EMPTY_SECTIONS:
            listed = ", ".join(empty)
            raise ValueError(
                # Diagnosis only, no re-emit imperative: `_parse_structured` folds this
                # text into its `reasons` and the CALLER supplies the corrective
                # instruction (`reemit_hint` — sentinel re-emit, structured payload, or
                # the tool loop's "call finalize again"). An imperative here would
                # contradict whichever one the session is actually using.
                f"{len(empty)} of {len(self.sections)} CV sections are empty ({listed}) — "
                "this is a heading-only outline, not a CV. Every section must carry its "
                "own content from the base CV in `text`, `items` or `entries`; a section "
                "name with a null or empty body is a dropped section."
            )
        return self

    @model_validator(mode="after")
    def _not_a_cover_letter(self, info: ValidationInfo) -> "CVDocument":
        # Content-kind guard: the cv_adjust stage must produce a résumé, not a letter. The
        # model has been observed to emit cover-letter prose into the CV shape; separate
        # per-stage schemas don't catch that (the letter satisfies "contact + ≥1 section").
        # Letter formulas (not sentiment words) in the CV text are the tell. Two+ matches is
        # decisive → reject → self-heal → fail if uncorrected (corruption, not thinness).
        #
        # Per-language: the formula list is selected via ``info.context["language"]`` (a
        # pipeline output may be in any configured language), defaulting to English when no
        # context is supplied (e.g. the CV Structure Editor / infer_structure call sites,
        # which don't carry a pipeline language concept).
        language = (info.context or {}).get("language", "en")
        hits = _letter_formula_re(language).findall(_cv_text_blob(self.sections))
        if len(hits) >= _LETTER_MATCH_THRESHOLD:
            sample = ", ".join(sorted({h.lower() for h in hits})[:3])
            raise ValueError(
                f"this reads as a cover letter, not a CV (letter phrasing: {sample}). The "
                "cv_adjust output must be a résumé — a contact block plus sections such as "
                "Summary, Experience, Skills, and Education with bullet points — not a "
                "letter addressed to an employer. Re-emit the candidate's CV as structured "
                "JSON; put any motivation prose in the cover-letter stage, not here."
            )
        return self

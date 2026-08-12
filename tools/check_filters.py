"""Run the skip filters over real lines from a real game.

Every string here was read off a Dark Souls III screenshot: equipment,
inventory, status, system menus and the bonfire HUD. Synthetic samples kept
missing what actually breaks - the first version of the "already in the
target language" filter passed a hand-written test and would have silently
dropped a third of this file, because Japanese writes 装備重量 and 初期化 in
kanji alone and no rule can tell those from Chinese.

    python tools/check_filters.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.ocr import (has_no_letters, is_already_target,  # noqa: E402
                     looks_like_noise, split_on_gaps,
                     strip_leading_artifact)


class Rect:
    """The shape winsdk hands back per recognised word."""

    def __init__(self, x, width, height):
        self.x, self.width, self.height = x, width, height


# Word rectangles measured off a live Nightreign character screen. Windows OCR
# returns one word per CJK character, so these are character boxes.
LINES = {
    # (x, width, height) per character, and where the line must be cut.
    "キャラクター選択": ([(573, 94, 102), (699, 82, 79), (816, 88, 99),
                   (929, 94, 101), (1050, 92, 104), (1169, 102, 21),
                   (1285, 108, 105), (1405, 109, 107)], []),
    "他者がアーツを": ([(5668, 73, 72), (5748, 72, 72), (5830, 72, 63),
                 (5911, 66, 65), (5989, 69, 14), (6073, 62, 63),
                 (6155, 60, 71)], []),
    # 、 then ア is the widest gap real text produced: 0.85 of line height.
    "時、アーツ": ([(6550, 70, 72), (6631, 21, 19), (6710, 67, 65),
                (6789, 69, 14), (6873, 62, 63)], []),
    # A stray mark sat 1.49 away from the word it had been merged with.
    "-力の感応": ([(5530, 16, 8), (5670, 82, 86), (5769, 79, 68),
                (5861, 85, 83), (5956, 87, 86)], [1]),
    # The icon left of アビリティ is the same size and right up against it;
    # nothing geometric separates it, and this records that it is not fixed.
    "針アビリティ": ([(5275, 108, 94), (5416, 80, 79), (5521, 72, 76),
                 (5623, 49, 84), (5704, 80, 80), (5808, 52, 69)], []),
}

# Leading characters no Japanese word starts with, so the recogniser put them
# there. ッスキル is the icon left of スキル.
STRIP_LEADING = [("ッスキル", "スキル"), ("ーアーツ", "アーツ"),
                 ("・目利き", "目利き"), ("ャマーキング", "マーキング")]
KEEP_WHOLE = ["アーツ", "スキル", "ステータス", "ー", "・"]
from glt.translate import is_commentary, is_wrong_language   # noqa: E402

# (reply, should it be dropped). The refusal is verbatim from the session log:
# the model declined and answered in English, and it is shorter than the line
# it was given, so the length rule below never sees it.
WRONG_LANGUAGE = [
    ("I appreciate you reaching out, but I'm not able to help with this "
     "translation", True),
    ("I cannot assist with that request.", True),
    ("Sorry, I can't help with translating this content.", True),
    # Real translations that happen to carry Latin, which must survive.
    ("第1天(星期二) / 攻击20防御11魔力20魔防16 / 5,000日元", False),
    ("HP MP Lv 5,000", False),
    ("黑暗之魂III / 60 帧/秒", False),
    ("进入 Mission，在床上睡觉吧！", False),
    ("状态", False),
]

# (source, reply) the model gave back. The first is verbatim off a screenshot:
# it was painted right across the status screen.
COMMENTARY = [
    ("青教", 'I cannot provide a translation because "青教" appears to be a '
             "proper noun or abbreviation (possibly referring to a specific "
             "organization, program, or concept) without sufficient context."),
    ("初期化", "抱歉，我无法确定这个词在游戏中的具体含义，因为它可能指初始化设置、"
              "重置角色数据或其他功能，需要更多上下文才能给出准确的翻译。"),
]

# Real translations, including the longest legitimate ones in the game.
NOT_COMMENTARY = [
    ("青教", "青教"),
    ("ステータス", "状态"),
    ("初期化", "恢复默认设置"),
    ("両手持ちで攻撃力が上がる", "双手持握时攻击力提升"),
    ("ロックオン対象死亡時の、対象の自動切替を設定します",
     "设置锁定目标死亡时是否自动切换目标"),
    ("最後に休息した篝火か、祭祀場の篝火に戻る",
     "返回最后休息的篝火，或祭祀场的篝火"),
    ("地面に投げ落とすと「こんにちは」と声を発する",
     "扔到地上时会发出「你好」的声音"),
]

# Japanese UI text. None of it may be skipped.
KEEP = [
    "装備", "装備重量", "重量割合", "キャラクターデータ", "レベル",
    "生命力", "集中力", "持久力", "体力", "筋力", "技量", "理力", "信仰", "運",
    "スタミナ", "強靭度", "発見力", "記憶スロット", "所持ソウル", "必要ソウル",
    "使用アイテム9", "螺旋剣の破片", "何度でも", "所持数", "格納数",
    "アイテム効果", "最後に休息した篝火か、祭祀場の篝火に戻る",
    "能力補正", "血赤の苔玉", "消費", "出血の蓄積を減らす",
    "インベントリ", "道具", "人面「こんにちは」",
    "地面に投げ落とすと「こんにちは」と声を発する",
    "ステータス", "誓約", "青教", "基礎力", "攻撃力", "防御力", "カット率",
    "右武器1", "左武器1", "物理", "対打撃", "対斬撃", "対刺突",
    "魔力", "炎", "雷", "闇", "耐性値", "防具耐性値",
    "出血", "毒", "冷気", "呪死",
    "装備スロットを選択してください", "操作するアイテムを選択してください",
    "キャラクターのステータスを確認します",
    "ゲーム終了", "ゲームを保存して、タイトル画面に戻ります", "ロスリック城",
    "カメラオプション", "カメラ操作　左右", "カメラ上下リセット", "カメラ速度",
    "カメラの壁自動回避", "演出カメラ", "初期化",
    "カメラの左右操作を設定します", "戻る", "決定", "はずす", "表示切替",
    "簡易表示", "ヘルプ", "メニュータブの切替", "説明表示",
    "大回復", "決別の黒水晶", "篝火で休息する",
    "ノーマル", "ON", "OFF",
]

# Pure numbers and separators. Translating these costs a request and returns
# them unchanged; on the equipment screen they are most of what is read.
DROP_NUMERIC = [
    "80.0 / 126.4", "63.3 %", "1854 / 1854", "377 / 377", "188",
    "655", "46051", "7031215", "212:31:01", "33.75", "199", "8",
    "1 /", "0 / 600", "99 / 99", "600 / 600", "233 / 41.282",
    "412 / 171", "346 / 108", "5", "70", "90", "2.8", "-",
]

# Texture the recogniser turned into characters, from an earlier session.
DROP_NOISE = [
    "、-。ー画履い・・をツ、、第日を一・男第、い箒物をツ Ｐ",
    # Ornament read as repeated characters. Off a Nightreign relic grid.
    # Too short for the punctuation-density test, which is why repetition is
    # judged separately.
    "把把把",
    "我第我第",
    # Scattered marks, which is what texture produces, as against the runs a
    # writer produces. Both shapes are punctuation; only the arrangement tells.
    "・・、、。ー・、。",
    "、-。ー・、-。ー・、-。",
]

# Real text that repeats, which must survive.
KEEP_REPEATED = [
    # Japanese reduplicates for real; a doubled kana pair is a word.
    "いろいろ", "だんだん", "そろそろ", "レベル", "ステータス",
    # An ellipsis is ordinary writing, and this whole line of dialogue was
    # being dropped because of one. Repetition is judged on Han only now.
    "うおぉ...まずいぞ壁がぎゅうぎゅう迫ってきて動けない...。",
    "………",
    # Heavy expressive punctuation is how dialogue is written, and counting it
    # as texture put this at a ratio of 0.55 against a 0.30 threshold.
    "な、なんだ……!!!",
    "「まさか…！？」",
    "そんな……ありえない！！",
    # The recogniser renders an ellipsis as middle dots, and a rule that
    # dropped any doubled ・ took this whole line off the screen.
    "「・・・・・、あっ、そういえば近所、に古い喫茶店があったわよね。",
    "そうか・・・わかった",
    "そうか・・・・・わかった",
    # A shout is written by repeating a kana, and it is a real line.
    "あああああ",
    "うおぉぉぉ",
    "ムムム",          # katakana ornament, now let through: see the note in ocr.py
]


def main() -> int:
    failures = []

    for line in KEEP:
        why = None
        if looks_like_noise(line):
            why = "被噪声过滤误杀"
        elif has_no_letters(line):
            why = "被数字过滤误杀"
        elif is_already_target(line, "zh-CN", "ja"):
            why = "被「已是目标语言」误杀"
        if why:
            failures.append(f"应保留却丢了 [{why}]: {line}")

    for line in DROP_NUMERIC:
        if not has_no_letters(line):
            failures.append(f"应跳过却保留（纯数字）: {line}")

    for line in DROP_NOISE:
        if not looks_like_noise(line):
            failures.append(f"应跳过却保留（纹理噪声）: {line}")

    for text, (boxes, want) in LINES.items():
        got = split_on_gaps([Rect(*b) for b in boxes])
        if got != want:
            failures.append(f"切分位置不对 {text!r}: 期望 {want}，实际 {got}")

    for text, want in STRIP_LEADING:
        got = strip_leading_artifact(text)
        if got != want:
            failures.append(f"行首杂字没剥干净 {text!r}: 期望 {want!r}，实际 {got!r}")

    for text in KEEP_WHOLE:
        if strip_leading_artifact(text) != text:
            failures.append(f"行首规则误伤了 {text!r}")

    for line in KEEP_REPEATED:
        if looks_like_noise(line):
            failures.append(f"重复模式误杀了真词: {line}")

    for reply, should_drop in WRONG_LANGUAGE:
        got = is_wrong_language(reply, "zh-CN")
        if got != should_drop:
            verb = "该丢却留下了" if should_drop else "该留却丢了"
            failures.append(f"{verb}: {reply[:46]!r}")

    for source, reply in COMMENTARY:
        if not is_commentary(source, reply):
            failures.append(f"模型的解释被当成译文画上去了: {source} -> {reply[:40]}…")

    for source, reply in NOT_COMMENTARY:
        if is_commentary(source, reply):
            failures.append(f"真译文被当成解释丢了: {source} -> {reply}")

    total = len(KEEP) + len(DROP_NUMERIC) + len(DROP_NOISE)
    print(f"真实游戏文本 {total} 条："
          f"保留 {len(KEEP)} / 跳过数字 {len(DROP_NUMERIC)} / "
          f"跳过噪声 {len(DROP_NOISE)}")
    print(f"模型答非所问 {len(COMMENTARY)} 条应丢弃，"
          f"真译文 {len(NOT_COMMENTARY)} 条应保留（含全游戏最长的几条）")
    drops = sum(1 for _, d in WRONG_LANGUAGE if d)
    print(f"模型用英文回绝 {drops} 条应丢弃，"
          f"带拉丁字母的真译文 {len(WRONG_LANGUAGE) - drops} 条应保留")
    if failures:
        print(f"\nFAIL - {len(failures)} 条不符合预期:")
        for item in failures:
            print("  -", item)
        return 1
    print("\nPASS - 没有误杀，也没有漏放")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

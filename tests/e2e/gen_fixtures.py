# -*- coding: utf-8 -*-
"""P1-02 端到端 CI 测试夹具生成器（自渲染，零版权风险）。

产出（默认写在本脚本所在目录）：
  horizontal.pdf —— 横排 6 页「扫描感」图版 PDF（200dpi，灰底噪声 + 页面微旋转）
  horizontal.expected.json —— 页数/必含字串/章数下限/结构键/CER 参考文本
  vertical.pdf   —— 竖排右起 4 页同上
  vertical.expected.json

文本全部为本项目自撰、仿公版诗文风格（公版体裁，非摘录），无任何版权风险；
渲染用固定种子（SEED），同版本 PIL/字号下确定性可复现（像素级一致）。

用法：
  python gen_fixtures.py [--outdir DIR] [--force]
退出码：0 成功；20 渲染失败（与 tools/e2e_check.py 退出码段一致）。

依赖：pillow + numpy（requirements-ci.txt 均已锁定）。
"""
import argparse
import json
import os
import sys

SEED = 20261126
DPI = 200
PAGE_W, PAGE_H = 1654, 2339  # A4 @ 200dpi

# ---------------------------------------------------------------- 文本（自撰）
# 横排：页 -> 行列表 (样式, 文本)；样式 title/sub/head/body
H_PAGES = [
    [("title", "横排测试·诗文小稿"),
     ("sub", "筑学书库端到端测试稿·公版自撰文字"),
     ("body", "本页文字为本项目自撰，仿公版诗文风格，无任何版权风险。"),
     ("body", "凡扫描成书，必验其字。故作此稿以测流水线："),
     ("body", "渲染、识别、结构、质检，层层相扣，一字之讹，CI当鸣。"),
     ("body", "稿成六页，横排右行，每页百数十言，请逐页识别。"),
     ("body", "又按：测试之稿，非人间书册，机读人读，一也。"),
     ("body", "使识别而无讹字，则吾夜分而后寝，可安眠矣。")],
    [("head", "第一章 春山杂咏"),
     ("body", "晨读"),
     ("body", "东方渐白雀声稀，竹外青山锁翠微。"),
     ("body", "展卷窗前风入座，一帘花影上书衣。"),
     ("body", "山行"),
     ("body", "石径萦回入翠微，白云深处有人归。"),
     ("body", "溪声不断松风起，斜日衔山鸟自飞。"),
     ("body", "晚眺"),
     ("body", "独立高台望落霞，炊烟几处认人家。"),
     ("body", "江天漠漠孤鸿远，风起芦花满岸沙。"),
     ("body", "观书"),
     ("body", "旧书百读意犹新，字里行间各有真。"),
     ("body", "莫笑书生痴绝处，一灯明灭伴三更。"),
     ("body", "对酒"),
     ("body", "小窗对酒写秋山，半纸墨痕浓淡间。"),
     ("body", "醉后不知更漏永，一窗凉月照芦花。")],
    [("body", "春雨"),
     ("body", "夜半春声响渐密，晓看庭树绿初匀。"),
     ("body", "农家知是田水足，平明荷锄问前津。"),
     ("body", "村居"),
     ("body", "茅屋三间傍水斜，篱边种豆复栽瓜。"),
     ("body", "客来一壶新焙酒，闲话蚕麻到暮鸦。"),
     ("body", "登楼"),
     ("body", "高楼百尺接云平，万里江山入眼明。"),
     ("body", "风起汀洲鸿雁下，夕阳人影满川晴。"),
     ("body", "田家"),
     ("body", "田家早起理晨装，露湿禾苗草叶香。"),
     ("body", "日午收工归饮歇，闲评雨水话蚕桑。"),
     ("body", "夜坐"),
     ("body", "夜坐西窗雨打筠，青灯一卷对贫身。"),
     ("body", "平生不解浮名利，只爱书中自在人。")],
    [("head", "第二章 读书杂记"),
     ("body", "山居之日，晨起理荒秽，午后读旧书。雨则闭门"),
     ("body", "抄经，晴则负薪入山。邻翁时过草庐，共话桑麻，"),
     ("body", "鸡鸣而后去。如此数年，虽无钟鼎之乐，亦有布"),
     ("body", "衣之欢。"),
     ("body", "凡为学之道，贵在有恒。一日暴之，十日寒之，"),
     ("body", "草木犹不能生，况学问乎。故君子择一事而终身"),
     ("body", "由之，不迁于外物，不惑于浮言。久之工夫纯熟，"),
     ("body", "自然左右逢其原。"),
     ("body", "学者所以成身也，非以夸世。今之人，朝习一艺"),
     ("body", "而暮欲成名，安能有所成？譬之种树，一日之苗，"),
     ("body", "欲其拱把，不可得已。故为学如养木，岁月深而"),
     ("body", "枝叶自茂也。")],
    [("body", "抄书之法，先净案几，次研墨，次展卷。字句有"),
     ("body", "讹，不敢臆改，阙所疑以俟后人。纸背有缝，则"),
     ("body", "别疏异同。一日所抄不过千字，然积之经年，亦"),
     ("body", "可盈箧而盈架矣。"),
     ("body", "村塾之教，以识字为先。三百字熟，乃授章句。"),
     ("body", "弟子晨集，各执册诵之，师巡而听，有误则正其"),
     ("body", "音。日暮乃散，寒暑不辍。乡子弟多由此通文墨。"),
     ("body", "古人云：三日不读书，则语言无味，面目可憎。"),
     ("body", "余少时家贫无书，借于邻家，手抄而诵之，窗影"),
     ("body", "摇雪，油灯耗芯，如是十年，遂粗知文理。"),
     ("body", "余友某生，善读书而不善记，每有所得，辄书于"),
     ("body", "手册，岁久成帙。或笑其拙，生曰：吾性急，心"),
     ("body", "手相应，惟恐其忘之也。人以为知言。")],
    [("body", "雨中过溪，水长没胫，石滑不可步。同行扶杖而"),
     ("body", "前，余独袒衣褰裳，踏流而渡。既济回望，烟树"),
     ("body", "苍茫，山色有无中，不觉大笑。从者怪而问之，"),
     ("body", "曰：世之可乐者多矣，特人不暇寻耳。"),
     ("body", "冬日曝背，献负之诚，虽微物也。农人岁晚，场"),
     ("body", "功既毕，乃修屋宇，缉垣墙，纳禾稼。妇功告成，"),
     ("body", "始治衣裳，为一岁之计。"),
     ("body", "凡校书之法，对目而下，参之以旁证。同书异本，"),
     ("body", "先校其行款；类书所引，以核其脱文。讹字以形"),
     ("body", "近者多，脱行以叶齐者辨。如此则十得七八，余"),
     ("body", "存疑焉。"),
     ("body", "此稿至此终。愿读之者如流水线之读扫描，字字"),
     ("body", "不误，页页无遗。")],
]

# 竖排：页 -> {"heads": [右起头部栏（大字）], "body": 正文流（满栏换左栏）}
V_PAGES = [
    {"heads": ["竖排测试杂记一卷", "第一章 书室杂课"],
     "body": ("凡书室日课，晨起磨墨一瓯，墨熟则心静，静则可抄可诵。"
              "上午抄书五百字，午后读史一段，夜温日间所诵，错字别书"
              "三十过，过则罚抄两篇。案头置石砚一枚，笔洗一，界尺一，"
              "皆粗具而已。凡写字先要心平，心不平则笔画躁，笔画躁则"
              "字俗，故日课以磨墨为第一义，墨成而心亦定矣。砚以石为"
              "贵，端歙为上，他石次之；笔贵健而齐，纸贵轻而滑。四者"
              "得，则书课半矣。同舍有老仆，亦知识字，日拾败纸，有字"
              "者必拾而投之字篓，敬字如此。先生见而嘉之，岁给米一石。"
              "每五日曝书一次，防霉蛀也。夏月尤须检点，否则纸鱼入书，"
              "一夜成孔。又规矩：凡借书于人，必录其名于册，还则勾之，"
              "恐其亡也。")},
    {"heads": [],
     "body": ("寒无熨纸之暖，手僵犹握笔；暑有挥扇之劳，汗渍不免污简。"
              "然皆不许废业。先生题壁曰恒，一字尽之矣。每五日一小测，"
              "默写校勘，优者给枣脯一握，劣者抄礼记两遍。同窗有兄弟"
              "二人，兄通而弟滞，兄夜半为弟讲之，弟乃悟。先生讲书，"
              "必令弟子复述，复不获则再讲，虽日昃不罢。或问其故，曰："
              "学贵自得，吾代其思，终非其有也。每岁正月朔，开笔砚，"
              "祀先师，礼也；仪虽简不敢废，盖重其所始也。又立课程牌"
              "于壁，曰读、曰抄、曰校、曰背，各有其时，虽风雨不易。"
              "月终会其多寡，多者书于册首以为劝。")},
    {"heads": ["第二章 山居杂记"],
     "body": ("出城东二十里入山，结庐两间，前有小圃，后有修竹。晨兴"
              "理荒秽，带月荷锄归，陶公之语，余私慕之。雨则闭门校书，"
              "晴则负薪入林，薪尽而书亦半矣。圃中种韭薤瓜瓞，取给不"
              "足，时采薇蕨佐餐。圃西有废渠，春水初生，辄坐其侧观鱼，"
              "鱼乐而人亦乐。山雨欲至，则白烟缕缕起于岭上，须臾满谷，"
              "读书其中，如隔世界。山中物产，笋也、蕨也、柿也、栗也，"
              "秋收毕，负至城市易盐米，往返一日，未尝以为劳。夜静闻"
              "水声绕舍皆是，枕上听之，如听琴筑。山中无更鼓，以月为"
              "度，月落则寝，鸡鸣则兴。")},
    {"heads": [],
     "body": ("邻翁时过草庐，共话桑麻，鸡鸣而后去。山中无历日，寒尽"
              "不知年，幸村人告余节气，春种秋收，不失其时。秋日偶下"
              "山县，见官道碑记，字多漫漶，乃归而录其可读之文，存一"
              "纸于箧。客有问余：山中寂寞乎？余笑不应，归而书之壁曰："
              "非无寂寞也，念此稿之未成也。此稿旧本题曰杂记一卷，盖"
              "取随见随录之义，无次第也，读者以意逆之，可矣。翁有孙"
              "年八岁，从余识字，三日诵得一诗，喜甚，归告其祖，祖笑"
              "而予之饼。稿至此终，愿竖排识别，右起成行，栏栏无讹，"
              "页页可核，则测试之功也。")},
]

H_MUST = {
    "1": ["横排测试", "自撰"],
    "2": ["春山杂咏", "石径萦回入翠微"],
    "3": ["夜半春声响渐密", "高楼百尺接云平"],
    "4": ["读书杂记", "贵在有恒"],
    "5": ["抄书之法", "三日不读书"],
    "6": ["雨中过溪", "字字不误"],
}
V_MUST = {
    "1": ["书室杂课", "墨熟则心静"],
    "2": ["先生题壁", "默写校勘"],
    "3": ["山居杂记", "结庐两间"],
    "4": ["山中无历日", "栏栏无讹"],
}

# convert 实际契约（pdf2book 产出不含顶层 pages；块含 type/text/page）
STRUCTURE_KEYS = ["title", "blocks", "stats", "chapters"]

_FONT_CANDS = [r"C:/Windows/Fonts/msyh.ttc", r"C:/Windows/Fonts/simsun.ttc",
               r"C:/Windows/Fonts/simhei.ttf",
               "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
               "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
               "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"]


def _font_maker():
    from PIL import ImageFont
    path = next((c for c in _FONT_CANDS if os.path.exists(c)), None)
    if not path:
        raise RuntimeError("找不到中文字体（msyh/simsun/simhei 均无）")

    def mk(size):
        try:
            return ImageFont.truetype(path, size, index=0)
        except TypeError:
            return ImageFont.truetype(path, size)
    return mk


def _page_bg():
    """纸白底 + 轻微高斯噪声（扫描感），固定种子保证确定性。

    注意：pdf2book.figure_regions 用「格内 <232 像素数≥6」判墨迹、std>24 判网点，
    底色均值必须显著高于 232 且噪声 std 远小于 24，否则整页会被误判为插图区，
    短而无标点的行（章题/诗题）会被 inside_fig 当图内标注删掉（2026-11-26 首跑踩坑）。
    """
    import numpy as np
    from PIL import Image
    rng = np.random.RandomState(SEED)
    noise = rng.normal(246, 4.0, (PAGE_H, PAGE_W))
    return Image.fromarray(noise.clip(236, 255).astype("uint8"), "L")


def _salt_pepper(img):
    """只撒白点（纸屑感）。黑点会触发 figure_regions 墨迹判据，禁用。"""
    import numpy as np
    from PIL import Image
    rng = np.random.RandomState(SEED + 7)
    a = np.asarray(img).astype("int16")
    m2 = rng.rand(*a.shape) < 0.00012
    a[m2] = 255
    return Image.fromarray(a.clip(0, 255).astype("uint8"), "L")


def _composite(bg, mask, angle):
    """文字遮罩层（白字黑底）微旋转后，以黑墨叠印到底图。"""
    from PIL import Image
    m = mask.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=0)
    ink = Image.new("L", m.size, 0)
    ox = (bg.width - m.width) // 2
    oy = (bg.height - m.height) // 2
    bg.paste(ink, (max(ox, 0), max(oy, 0)), m)
    return bg


def render_horizontal(outdir):
    from PIL import Image, ImageDraw
    mk = _font_maker()
    pages, refs = [], {}
    for i, lines in enumerate(H_PAGES, 1):
        bg = _page_bg()
        mask = Image.new("L", (PAGE_W, PAGE_H), 0)
        d = ImageDraw.Draw(mask)
        y = 210
        for style, text in lines:
            if style == "title":
                f = mk(84)
                w = d.textlength(text, font=f)
                d.text(((PAGE_W - w) / 2, y), text, font=f, fill=250)
                y += 150
            elif style == "sub":
                f = mk(50)
                w = d.textlength(text, font=f)
                d.text(((PAGE_W - w) / 2, y), text, font=f, fill=245)
                y += 130
            elif style == "head":
                f = mk(62)
                y += 20
                d.text((170, y), text, font=f, fill=250)
                y += 120
            else:
                f = mk(44)
                d.text((190, y), text, font=f, fill=250)
                y += 92
        angle = [-0.25, 0.2, -0.15, 0.28, -0.2, 0.15][i - 1]
        bg = _composite(bg, mask, angle)
        pages.append(_salt_pepper(bg).convert("RGB"))
        refs[str(i)] = "".join(t for _, t in lines)
    pdf = os.path.join(outdir, "horizontal.pdf")
    pages[0].save(pdf, "PDF", save_all=True, append_images=pages[1:],
                  resolution=float(DPI))
    return pdf, refs


def render_vertical(outdir):
    from PIL import Image, ImageDraw
    # 字径 44px / 栏距 66px（200dpi）：实测 vertical_ocr 列切分与页归属最稳的配置。
    # 注：合成字体（雅黑细笔画）会让 PP-OCRv5 mobile rec 对「章/晨/翁/贵」等复杂字
    # 出现拆字幻觉（章→立早），页级 CER 约 0.08-0.13，仍远低于 0.35 容差；
    # 真实刻本扫描件（粗笔画大字）无此现象。must_contain 已避开拆字高危片段。
    mk = _font_maker()
    COL_W, CHAR, TOP, BOTTOM = 66, 44, 230, PAGE_H - 200
    pages, refs = [], {}
    for i, spec in enumerate(V_PAGES, 1):
        bg = _page_bg()
        mask = Image.new("L", (PAGE_W, PAGE_H), 0)
        d = ImageDraw.Draw(mask)
        col = 0

        def x_of(c, pad=6):
            return PAGE_W - 170 - c * COL_W - CHAR - pad

        for h in spec["heads"]:
            f = mk(54)
            yy = TOP - 40
            for ch in h:
                if ch == " ":
                    yy += 22
                    continue
                d.text((x_of(col) - 4, yy), ch, font=f, fill=250)
                yy += 62
            col += 1
        f = mk(CHAR)
        yy = TOP
        for ch in spec["body"]:
            d.text((x_of(col), yy), ch, font=f, fill=250)
            yy += CHAR + 8
            if yy > BOTTOM:
                col += 1
                yy = TOP
        angle = [0.22, -0.18, 0.25, -0.2][i - 1]
        bg = _composite(bg, mask, angle)
        pages.append(_salt_pepper(bg).convert("RGB"))
        refs[str(i)] = "".join(spec["heads"]) + spec["body"]
    pdf = os.path.join(outdir, "vertical.pdf")
    pages[0].save(pdf, "PDF", save_all=True, append_images=pages[1:],
                  resolution=float(DPI))
    return pdf, refs


def _expected(name, refs):
    h = name == "horizontal"
    pages = H_PAGES if h else V_PAGES
    return {
        "fixture": name + ".pdf",
        "orientation": name,
        "dpi": DPI,
        "pages": len(pages),
        "chapters_min": 1,
        "chars_min_total": 1000,
        "structure_keys": STRUCTURE_KEYS,
        "must_contain": H_MUST if h else V_MUST,
        "cer": {"tolerance": 0.35, "unit": "per_page_cjk_normalized", "refs": refs},
        "pipeline": {"vertical": not h,
                     "title": "横排测试诗文小稿" if h else "竖排测试杂记一卷"},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="端到端 CI 测试夹具生成（自渲染公版文本）")
    ap.add_argument("--outdir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--force", action="store_true", help="已存在也重新渲染像素")
    args = ap.parse_args(argv)
    os.makedirs(args.outdir, exist_ok=True)
    try:
        import numpy, PIL  # noqa: F401
        hp, hrefs = render_horizontal(args.outdir)
        vp, vrefs = render_vertical(args.outdir)
        for p in (hp, vp):
            print("[render] %s (%d bytes)" % (p, os.path.getsize(p)))
        for name, refs in (("horizontal", hrefs), ("vertical", vrefs)):
            ep = os.path.join(args.outdir, name + ".expected.json")
            with open(ep, "w", encoding="utf-8", newline="\n") as f:
                json.dump(_expected(name, refs), f, ensure_ascii=False, indent=1)
            print("[render] %s" % ep)
        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("[render] 失败：%s" % e)
        return 20


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

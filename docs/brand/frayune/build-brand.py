"""Rebuild FRAYUNE SVG assets using project-drawn geometry and saved Noto outlines."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEST = ROOT / "web/brand"
HERE = Path(__file__).resolve().parent
# Three asymmetric leaves: a rising spine, a broad page, and a detached lower fold.
SYMBOL = "M14 96V28Q14 18 24 18H41V57C41 77 31 89 14 96Z M34 110C55 86 61 63 53 37L79 27C96 65 76 101 34 110Z M77 108C95 91 103 72 101 49L116 64C122 83 108 105 77 108Z"
# Custom uppercase geometric lettering. No font data used in English wordmark.
GLYPHS = {
    "F": "M0 0H25V6H7V15H22V21H7V36H0Z",
    "R": "M0 0H16C25 0 29 4 29 11C29 17 26 20 22 21L31 36H23L14 22H7V36H0Z M7 6V16H15C20 16 22 14 22 11C22 8 20 6 15 6Z",
    "A": "M0 36L13 0H21L34 36H26L23 27H11L8 36Z M13 21H21L17 8Z",
    "Y": "M0 0H8L17 15L26 0H34L21 22V36H13V22Z",
    "U": "M0 0H7V23C7 29 10 31 15 31C20 31 23 29 23 23V0H30V23C30 32 25 37 15 37C5 37 0 32 0 23Z",
    "N": "M0 36V0H7L24 24V0H31V36H24L7 12V36Z",
    "E": "M0 0H26V6H7V15H23V21H7V30H27V36H0Z",
}


def wordmark():
    return "".join(
        f'<path transform="translate({i * 43} 0)" fill-rule="evenodd" d="{GLYPHS[c]}"/>'
        for i, c in enumerate("FRAYUNE")
    )


def symbol():
    return f'<path d="{SYMBOL}"/>'


def svg(body, box="0 0 128 128", color="#171918", title="帧屿集 FRAYUNE"):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}" fill="{color}" role="img" aria-labelledby="title"><title id="title">{title}</title>{body}</svg>\n'


def main():
    cn = (HERE / "chinese-paths.svgfrag").read_text()
    for name, color in [("logo", "#171918"), ("logo-ink", "#171918"), ("logo-white", "#FFFFFF")]:
        (DEST / f"{name}.svg").write_text(svg(symbol(), color=color))
    (DEST / "wordmark.svg").write_text(svg(wordmark(), "0 0 288 38"))
    (DEST / "name-zh.svg").write_text(svg(cn, "0 0 90 32"))
    (DEST / "favicon.svg").write_text(
        svg(
            '<rect x="1" y="1" width="126" height="126" rx="28" fill="#171918" stroke="#FFF" stroke-opacity=".25"/><g transform="translate(12 12) scale(.82)" fill="#FFF">'
            + symbol()
            + "</g>"
        )
    )
    lockup = (
        symbol()
        + '<g transform="translate(156 35) scale(1.07)">'
        + wordmark()
        + '</g><g transform="translate(157 81)">'
        + cn
        + "</g>"
    )
    (DEST / "logo-lockup.svg").write_text(svg(lockup, "0 0 476 128"))
    (DEST / "logo-lockup-white.svg").write_text(svg(lockup, "0 0 476 128", color="#FFFFFF"))
    tiles = ""
    for x, bg, fg, label in [
        (76, "#FFFFFF", "#171918", "LIGHT"),
        (518, "#171918", "#FFFFFF", "DARK"),
        (960, "#EAEAE7", "#171918", "APP ICON"),
    ]:
        tiles += f'<rect x="{x}" y="540" width="404" height="266" rx="12" fill="{bg}"/>'
        if label == "APP ICON":
            tiles += (
                f'<rect x="{x + 144}" y="582" width="116" height="116" rx="26" fill="#171918"/><g transform="translate({x + 154} 592) scale(.75)" fill="#FFF">'
                + symbol()
                + "</g>"
            )
        else:
            tiles += f'<g transform="translate({x + 138} 584)" fill="{fg}">' + symbol() + "</g>"
        tiles += f'<text x="{x + 24}" y="782" fill="{fg}" font-size="11" letter-spacing="2">{label}</text>'
    board = (
        '<rect width="1440" height="900" fill="#F5F5F3"/><g font-family="Arial,sans-serif"><text x="76" y="65" font-size="12" letter-spacing="3">FRAYUNE / VISUAL IDENTITY</text><text x="1170" y="65" font-size="12" letter-spacing="2">MONOCHROME</text><g transform="translate(166 151) scale(2.2)">'
        + symbol()
        + '</g><g transform="translate(527 237) scale(2.5)">'
        + wordmark()
        + '</g><g transform="translate(530 354) scale(1.35)">'
        + cn
        + '</g><text x="531" y="436" font-size="12" letter-spacing="3">AI VISUAL CREATION STUDIO</text><path d="M76 496H1364" stroke="#D4D4D0"/>'
        + tiles
        + '<text x="76" y="858" fill="#777" font-size="11" letter-spacing="2">UNFOLD YOUR NEXT FRAME.</text></g>'
    )
    (HERE / "brand-identity.svg").write_text(svg(board, "0 0 1440 900"))


if __name__ == "__main__":
    main()

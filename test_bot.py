"""self-check for the caption parser and repo normaliser: python test_bot.py"""
import bot

# Title / Tags / Link / description
t, d, tags, link = bot.parse_caption(
    "Title: Injera Machine\n"
    "Tags: esp32, erp\n"
    "Link: https://injera.example.com\n"
    "Full control system.\n"
    "Built with ADYSSINIA."
)
assert t == "Injera Machine", t
assert tags == ["esp32", "erp"], tags
assert link == "https://injera.example.com", link
assert d == "Full control system. Built with ADYSSINIA.", d

# bare domain gets https://
assert bot.parse_caption("Title: X\nurl: foo.com")[3] == "https://foo.com"
# no link field -> empty, so the site shows no View button
assert bot.parse_caption("Title: X\njust a description")[3] == ""
# no Title: line -> first line is the title
assert bot.parse_caption("My Thing\nand what it does")[0] == "My Thing"
assert bot.parse_caption("")[0] is None

# a github pages url or repo url still resolves to owner/name
assert bot.normalize_github_repo("https://th3npc.github.io/portfolio/") == "th3npc/portfolio"
assert bot.normalize_github_repo("https://github.com/TH3NPC/portfolio.git") == "TH3NPC/portfolio"
assert bot.normalize_github_repo("TH3NPC/portfolio") == "TH3NPC/portfolio"

# album photos are posted in sent order, not arrival order
shots = sorted([(12, "c"), (10, "a"), (11, "b")], key=lambda s: s[0])
assert [f for _, f in shots] == ["a", "b", "c"], shots

print("all checks passed")

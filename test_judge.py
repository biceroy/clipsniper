"""Self-check for the big-win rule and the bonus merge, using moments from a real scan."""
import clipsniper as cs

J = lambda cfg={}, **m: cs.judge(m, cfg)

r = J(bet_num=1, win_num=1273.65, stage="bonus_end")
assert r["kind"] == "big_win" and r["clip_worthy"] and r["multiplier"] == "1274x", r
assert J(bet_num=150, win_num=3666, banner="SENSATIONAL")["clip_worthy"]
assert J({"min_win": 5000}, bet_num=150, win_num=3666, banner="SENSATIONAL")["kind"] == "nothing"
assert J(bet_num=9, win_num=35.40, stage="base")["kind"] == "nothing"
assert J(bet_num=None, win_num=None, banner="MEGA WIN")["kind"] == "big_win"
assert J(bet_num=1, win_num=20, banner="BIG WIN")["kind"] == "nothing"
assert J(bet_num=1.4, win_num=264.6, buy_cost=500, stage="bonus")["kind"] == "bonus"          # a $500 super that paid $264 is a loss, not 189x
assert J(bet_num=0.8, win_num=712.4, buy_cost=220, stage="bonus")["kind"] == "bonus"          # 890x the bet, but only 3x the $220 price
assert J(bet_num=0.5, win_num=1068.5, buy_cost=200, stage="bonus_end")["kind"] == "big_win"   # a $200 buy that paid 5x and $1,000+
assert J(bet_num=0.5, win_num=1068.5, stage="other")["kind"] == "nothing"                     # stale WIN field under a paytable
assert J(laugh=8, stage="other")["kind"] == "funny"
assert J(laugh=7, stage="other")["kind"] == "nothing"
assert J(laugh=3, talk_laugh=8, stage="bonus")["kind"] == "funny"  # transcript pick outvotes a dull frame read
assert J(laugh=2, hype=10, stage="lobby")["kind"] == "funny"       # "$26,000 cash out Jesus Christ"
assert J(laugh=2, hype=8, stage="lobby")["kind"] == "nothing"
assert J(laugh=6, hype=7, stage="base")["kind"] == "nothing"
assert J(bet_num=1, win_num=40, stage="bonus") == {"kind": "bonus", "clip_worthy": False, "multiplier": "40x"}

assert cs.same_slot("Soaked By Sea Men", "Soaked By Seamen") and cs.same_slot("Gates of Olympus 2500", "Gates of Olympus")
assert cs.same_slot("", "Rotten") and not cs.same_slot("Rotten", "Pug Life")

rec = lambda t, win, slot="Toad Town", stage="bonus", score=20: {"t": t, "slot": slot, "stage": stage, "win_num": win, "win_amount": str(win),
                                                                 "score": score, "start": t - 20, "end": t + 30}
m = cs.merge([rec(8006, 1), rec(8085, 95.3, "Toad  town!", score=30), rec(8255, 881.9)])
assert len(m) == 1 and m[0]["win_num"] == 881.9 and m[0]["win_amount"] == "881.9" and m[0]["merged"] == [8006, 8085, 8255], m
assert m[0]["t"] == 8085 and m[0]["start"] == 8065 and m[0]["end"] == 8115, m  # the loudest spike is the moment, the range stays a clip
assert len(cs.merge([rec(8255, 881.9), rec(8355, 5, stage="base")])) == 2
assert len(cs.merge([rec(100, 5), rec(500, 9)])) == 2
assert len(cs.merge([rec(100, 143.5, stage="bonus_end"), rec(200, 1068.5, stage="bonus_trigger")])) == 2  # a rebuy after the summary is a new bonus
m = cs.merge([rec(9234, 1068.5), rec(9325, 1068.5, stage="base", score=24)])  # the WIN field under a paytable still shows the bonus total
assert len(m) == 1 and m[0]["stage"] == "bonus" and m[0]["t"] == 9325, m
assert len(cs.merge([rec(7848, 216.4), rec(7936, 1057.8, stage="base")])) == 2  # a different number on the base game is its own moment
print("ok")

import unittest

from egc.cross import (
    Constraint,
    CrossGraph,
    Link,
    Node,
    build_graph,
    cross_signals,
    normalize_person,
    parse_person_ref,
    solve_graph,
)
from egc.cross_backtest import link_stats, run_link


def mk_market(cid, question, mid, spread=0.01, volume=1_000_000.0):
    half = spread / 2.0
    return {
        "condition_id": cid,
        "question": question,
        "tokens": [f"{cid}-y", f"{cid}-n"],
        "outcomes": ["Yes", "No"],
        "bid": mid - half,
        "ask": mid + half,
        "mid": mid,
        "volume": volume,
        "liquidity": volume,
        "end_date": "2028-11-07",
        "active": True,
    }


def mk_event(eid, title, markets, neg_risk=True):
    return {
        "id": eid,
        "slug": eid,
        "title": title,
        "neg_risk": neg_risk,
        "markets": markets,
        "volume": sum(m["volume"] for m in markets),
        "liquidity": sum(m["liquidity"] for m in markets),
    }


def node(key, prior, weight=1.0, market=None):
    return Node(
        key=key,
        market=market,
        event_id="e",
        event_title="t",
        prior=prior,
        weight=weight,
    )


class EntityTest(unittest.TestCase):
    def test_initials_collide(self):
        self.assertEqual(normalize_person("J.D. Vance"), normalize_person("JD Vance"))

    def test_junior_is_not_the_same_person(self):
        self.assertNotEqual(
            normalize_person("Donald Trump"), normalize_person("Donald Trump Jr.")
        )

    def test_nomination_and_presidency_refs(self):
        nom = parse_person_ref(
            "Will J.D. Vance win the 2028 Republican presidential nomination?"
        )
        pres = parse_person_ref("Will JD Vance win the 2028 US Presidential Election?")
        self.assertEqual(nom["person"], pres["person"])
        self.assertEqual(nom["office"], "nomination")
        self.assertEqual(pres["office"], "president")
        self.assertEqual(nom["party"], "R")


class GraphTest(unittest.TestCase):
    def test_nomination_implies_presidency(self):
        nominee = mk_event(
            "dem",
            "Democratic Presidential Nominee 2028",
            [
                mk_market(
                    "nom-newsom",
                    "Will Gavin Newsom win the 2028 Democratic presidential nomination?",
                    0.5,
                ),
                mk_market(
                    "nom-aoc",
                    "Will Alexandria Ocasio-Cortez win the 2028 Democratic presidential nomination?",
                    0.3,
                ),
                mk_market(
                    "nom-pete",
                    "Will Pete Buttigieg win the 2028 Democratic presidential nomination?",
                    0.2,
                ),
            ],
        )
        president = mk_event(
            "pres",
            "Presidential Election Winner 2028",
            [
                mk_market(
                    "pres-newsom",
                    "Will Gavin Newsom win the 2028 US Presidential Election?",
                    0.6,
                ),
                mk_market(
                    "pres-aoc",
                    "Will Alexandria Ocasio-Cortez win the 2028 US Presidential Election?",
                    0.2,
                ),
                mk_market(
                    "pres-other",
                    "Will another candidate win the 2028 US Presidential Election?",
                    0.2,
                ),
            ],
        )
        graph = build_graph([nominee, president])
        labels = [c.label for c in graph.constraints]
        self.assertTrue(
            any("nomination->president:gavin newsom" in label for label in labels)
        )
        solution = solve_graph(graph)
        q = solution["q"]
        self.assertLessEqual(q["pres-newsom"], q["nom-newsom"] + 1e-6)

    def test_candidate_event_gets_latent_other(self):
        event = mk_event(
            "who",
            "Who will win the 2028 Example?",
            [
                mk_market("a", "Will Alice win the 2028 Example?", 0.4),
                mk_market("b", "Will Bob win the 2028 Example?", 0.3),
                mk_market("c", "Will Carol win the 2028 Example?", 0.2),
            ],
        )
        graph = build_graph([event])
        latent = [k for k, n in graph.nodes.items() if n.latent]
        self.assertEqual(len(latent), 1)
        solution = solve_graph(graph)
        total = (
            sum(solution["q"][k] for k in ("a", "b", "c")) + solution["q"][latent[0]]
        )
        self.assertAlmostEqual(total, 1.0, places=5)

    def test_balance_of_power_marginals(self):
        bop = mk_event(
            "bop",
            "Balance of Power: 2026 Midterms",
            [
                mk_market("dd", "2026 Balance of Power: D Senate, D House", 0.5),
                mk_market("dr", "2026 Balance of Power: D Senate, R House", 0.1),
                mk_market("rd", "2026 Balance of Power: R Senate, D House", 0.25),
                mk_market("rr", "2026 Balance of Power: R Senate, R House", 0.15),
            ],
        )
        senate = mk_event(
            "sen",
            "Which party will win the Senate in 2026?",
            [
                mk_market(
                    "sen-d",
                    "Will the Democratic Party control the Senate after the 2026 Midterm elections?",
                    0.55,
                ),
                mk_market(
                    "sen-r",
                    "Will the Republican Party control the Senate after the 2026 Midterm elections?",
                    0.45,
                ),
            ],
        )
        house = mk_event(
            "hou",
            "Which party will win the House in 2026?",
            [
                mk_market(
                    "hou-d",
                    "Will the Democratic Party control the House after the 2026 Midterm elections?",
                    0.75,
                ),
                mk_market(
                    "hou-r",
                    "Will the Republican Party control the House after the 2026 Midterm elections?",
                    0.25,
                ),
            ],
        )
        graph = build_graph([bop, senate, house])
        labels = [c.label for c in graph.constraints]
        self.assertIn("bop:senate-D:2026", labels)
        self.assertIn("bop:house-D:2026", labels)
        q = solve_graph(graph)["q"]
        self.assertAlmostEqual(q["dd"] + q["dr"], q["sen-d"], places=4)
        self.assertAlmostEqual(q["dd"] + q["rd"], q["hou-d"], places=4)

    def test_cross_event_date_ladder(self):
        early = mk_event(
            "early",
            "Will X happen by October 31, 2026?",
            [mk_market("early-m", "Will X happen by October 31, 2026?", 0.3)],
        )
        late = mk_event(
            "late",
            "Will X happen by December 31, 2026?",
            [mk_market("late-m", "Will X happen by December 31, 2026?", 0.2)],
        )
        graph = build_graph([early, late])
        labels = [c.label for c in graph.constraints]
        self.assertTrue(any(label.startswith("date-cross:") for label in labels))
        q = solve_graph(graph)["q"]
        self.assertLessEqual(q["early-m"], q["late-m"] + 1e-6)


class SolverTest(unittest.TestCase):
    def test_pair_is_projected_onto_the_inequality(self):
        graph = CrossGraph()
        graph.nodes["p"] = node("p", 0.6)
        graph.nodes["n"] = node("n", 0.5)
        graph.add_constraint(
            Constraint("le", {"p": 1.0, "n": -1.0}, 0.0, "pair", "nomination")
        )
        q = solve_graph(graph)["q"]
        self.assertLessEqual(q["p"], q["n"] + 1e-6)
        self.assertAlmostEqual(q["p"], q["n"], places=3)

    def test_violation_produces_two_sided_signals(self):
        graph = CrossGraph()
        mp = mk_market("p", "Will P win?", 0.6)
        mn = mk_market("n", "Will N win?", 0.5)
        graph.nodes["p"] = node("p", 0.6, market=mp)
        graph.nodes["n"] = node("n", 0.5, market=mn)
        graph.add_constraint(
            Constraint("le", {"p": 1.0, "n": -1.0}, 0.0, "pair", "nomination")
        )
        solution = solve_graph(graph)
        sigs = cross_signals(
            graph, solution, min_edge=0.001, max_spread=0.05, min_volume=1000
        )
        self.assertEqual({s["side"] for s in sigs}, {"YES", "NO"})
        self.assertTrue(all(s["links"] == ["pair"] for s in sigs))

    def test_coherent_pair_has_no_signal(self):
        graph = CrossGraph()
        mp = mk_market("p", "Will P win?", 0.5)
        mn = mk_market("n", "Will N win?", 0.5)
        graph.nodes["p"] = node("p", 0.5, market=mp)
        graph.nodes["n"] = node("n", 0.5, market=mn)
        graph.add_constraint(
            Constraint("le", {"p": 1.0, "n": -1.0}, 0.0, "pair", "nomination")
        )
        solution = solve_graph(graph)
        self.assertEqual(
            cross_signals(
                graph, solution, min_edge=0.001, max_spread=0.05, min_volume=1000
            ),
            [],
        )


class BacktestTest(unittest.TestCase):
    def _graph_and_series(self, p_path, n_price=0.5):
        graph = CrossGraph()
        mp = mk_market("p", "Will P win?", p_path[0], spread=0.002)
        mn = mk_market("n", "Will N win?", n_price, spread=0.002)
        graph.nodes["p"] = node("p", p_path[0], market=mp)
        graph.nodes["n"] = node("n", n_price, market=mn)
        link = Link("nomination", "pair", {"p": 1.0, "n": -1.0}, 0.0, "le")
        series = {
            "p": [(i * 300.0, price) for i, price in enumerate(p_path)],
            "n": [(i * 300.0, n_price) for i in range(len(p_path))],
        }
        return graph, link, series

    def test_converging_gap_is_profitable(self):
        p_path = [0.65 - 0.15 * i / 99 for i in range(100)]
        graph, link, series = self._graph_and_series(p_path)
        trades = run_link(
            link,
            graph,
            series,
            entry=0.05,
            exit_gap=0.01,
            take_profit=0.5,
            stop_loss=0.5,
            max_bars=50,
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "converged")
        self.assertGreater(trades[0]["ret"], 0.0)

    def test_link_stats_reports_cost_and_half_life(self):
        p_path = [0.65 - 0.15 * i / 99 for i in range(100)]
        graph, link, series = self._graph_and_series(p_path)
        stats = link_stats(link, graph, series)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["legs"], 2)
        self.assertGreater(stats["cost_fraction"], 0.0)
        self.assertIn("half_life_bars", stats)

    def test_persistent_gap_times_out(self):
        p_path = [0.65] * 100
        graph, link, series = self._graph_and_series(p_path)
        trades = run_link(
            link,
            graph,
            series,
            entry=0.05,
            exit_gap=0.01,
            take_profit=0.5,
            stop_loss=0.5,
            max_bars=10,
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "time")
        self.assertEqual(trades[0]["bars"], 10)


if __name__ == "__main__":
    unittest.main()

"""LangGraph state machine — SQL + SPARQL paths (M3).

START → retrieve → policy_check ─[block]─► compose ─► END
                                └[allow]─► plan ─┬[sql]──► generate_sql   → validate_sql
                                                 └[sparql]► generate_sparql→ validate_sparql
   validate_* ─[repair]─► repair_prep ─┬► generate_sql / generate_sparql
              ─[compose]─► compose       (by target_lang)
              ─[score]──► score_query ─[repair]─► repair_prep
                                       ─[execute_sql|execute_sparql]─► execute_* →
                                           compose_insights → compose → END
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from nodes import (
    State,
    compose,
    compose_insights,
    execute_sparql,
    execute_sql,
    generate_sparql,
    generate_sql,
    plan_query,
    policy_check,
    repair_prep,
    retrieve,
    route_after_plan,
    route_after_policy,
    route_after_repair,
    route_after_score,
    route_after_validate_sparql,
    route_after_validate_sql,
    score_query,
    validate_sparql,
    validate_sql,
)


def build_graph():
    g = StateGraph(State)

    g.add_node("retrieve", retrieve)
    g.add_node("policy_check", policy_check)
    g.add_node("plan", plan_query)
    g.add_node("generate_sql", generate_sql)
    g.add_node("generate_sparql", generate_sparql)
    g.add_node("validate_sql", validate_sql)
    g.add_node("validate_sparql", validate_sparql)
    g.add_node("score_query", score_query)
    g.add_node("repair_prep", repair_prep)
    g.add_node("execute_sql", execute_sql)
    g.add_node("execute_sparql", execute_sparql)
    g.add_node("compose_insights", compose_insights)
    g.add_node("compose", compose)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "policy_check")
    g.add_conditional_edges("policy_check", route_after_policy,
                            {"allow": "plan", "block": "compose"})
    g.add_conditional_edges("plan", route_after_plan,
                            {"sql": "generate_sql", "sparql": "generate_sparql"})
    g.add_edge("generate_sql", "validate_sql")
    g.add_edge("generate_sparql", "validate_sparql")
    g.add_conditional_edges("validate_sql", route_after_validate_sql,
                            {"score": "score_query", "repair": "repair_prep", "compose": "compose"})
    g.add_conditional_edges("validate_sparql", route_after_validate_sparql,
                            {"score": "score_query", "repair": "repair_prep", "compose": "compose"})
    g.add_conditional_edges("score_query", route_after_score,
                            {"execute_sql": "execute_sql", "execute_sparql": "execute_sparql",
                             "repair": "repair_prep"})
    g.add_conditional_edges("repair_prep", route_after_repair,
                            {"sql": "generate_sql", "sparql": "generate_sparql"})
    g.add_edge("execute_sql", "compose_insights")
    g.add_edge("execute_sparql", "compose_insights")
    g.add_edge("compose_insights", "compose")
    g.add_edge("compose", END)

    return g.compile()


app_graph = build_graph()

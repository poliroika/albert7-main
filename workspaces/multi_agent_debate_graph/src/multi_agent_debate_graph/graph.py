

import gmas
from gmas.builder import GraphBuilder


def build_debate_skeleton():
    builder = GraphBuilder()
    builder.add_agent(
        "framer",
        persona="Clarifies the question and success criteria.",
        description="Frames the debate scope.",
    )
    builder.add_agent(
        "advocate",
        persona="Argues in favour of the thesis.",
        description="Pro side.",
    )
    builder.add_agent(
        "skeptic",
        persona="Argues against or stresses weaknesses.",
        description="Con side.",
    )
    builder.add_agent(
        "synthesizer",
        persona="Merges pro/con into a balanced view.",
        description="Synthesis.",
    )
    builder.add_edge("framer", "advocate")
    builder.add_edge("framer", "skeptic")
    builder.add_edge("advocate", "synthesizer")
    builder.add_edge("skeptic", "synthesizer")
    builder.set_start_node("framer")
    builder.set_end_node("synthesizer")
    return builder.build()

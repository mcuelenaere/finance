import pygal
import sys
from collections import defaultdict
from itertools import groupby
from finance.config import parse_config
from pygal.style import DefaultStyle


def render_stacked_chart(transactions, time_format: str, title: str):
    # group by time unit
    x_labels = []
    series = defaultdict(dict)
    for time_unit, batch in groupby(transactions, key=lambda t: t.timestamp.strftime(time_format)):
        x_labels.append(time_unit)
        for category, batch in groupby(batch, key=lambda t: categorizer.categorize(t)):
            series[category][time_unit] = sum(t.amount for t in batch)

    # generate graph
    graph = pygal.StackedBar(
        value_formatter=lambda x: f'€ {x}',
        width=1200,
        height=600,
        margin=50,
        legend_at_bottom=True,
        tooltip_border_radius=3,
        style=DefaultStyle(tooltip_font_size=8)
    )
    graph.x_labels = x_labels
    graph.title = title
    for category, time_units in series.items():
        graph.add(category, [time_units.get(x, None) for x in x_labels])
    return graph

if __name__ == "__main__":
    config = parse_config(sys.argv[1])
    categorizer = config.categorizer()
    transactions = sorted(config.transactions(), key=lambda t: (t.timestamp, t.source.number))

    render_stacked_chart(transactions, '%Y-%m', title='Transactions per month').render_to_file('per_month.svg')
    render_stacked_chart(transactions, '%Y', title='Transactions per year').render_to_file('per_year.svg')

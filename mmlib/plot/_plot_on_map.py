from typing import Any
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# --- Types ---
type Lat = float
type Lon = float
type Point = tuple[Lat, Lon]


# --- Private Helper Functions ---
def _prepare_dataframe(
    original: list[Point],
    calculated: list[Point],
    original_label: str,
    calculated_label: str,
) -> pd.DataFrame:
    """Prepare DataFrame from original and calculated trajectories."""
    flat_original = [
        (lat, lon, original_label, i) for i, (lat, lon) in enumerate(original)
    ]
    flat_calculated = [
        (lat, lon, calculated_label, i) for i, (lat, lon) in enumerate(calculated)
    ]

    return pd.DataFrame(
        flat_original + flat_calculated,
        columns=["lat", "lon", "type", "row_num"],
    )


def _calculate_map_center(
    df: pd.DataFrame,
    center_lat: float | None,
    center_lon: float | None,
) -> tuple[float, float]:
    """Calculate map center from DataFrame or use provided values."""
    if center_lat is None or center_lon is None:
        return float(df["lat"].mean()), float(df["lon"].mean())
    return center_lat, center_lon


def _create_base_figure(
    df: pd.DataFrame,
    original_label: str,
    calculated_label: str,
    zoom: int,
    title: str,
) -> go.Figure:
    """Create base figure using Plotly Express scatter_map."""
    return px.scatter_map(
        df,
        lat="lat",
        lon="lon",
        color="type",
        zoom=zoom,
        height=700,
        title=title,
        color_discrete_map={original_label: "red", calculated_label: "blue"},
    )


def _add_line_traces(
    fig: go.Figure,
    df: pd.DataFrame,
    original_label: str,
    calculated_label: str,
    show_original_line: bool,
) -> None:
    """Add line traces for trajectories."""
    for type_ in [original_label, calculated_label]:
        # Skip original line if not requested
        if type_ == original_label and not show_original_line:
            continue

        subset = df[df["type"] == type_].sort_values(by=["row_num"])
        color = "red" if type_ == original_label else "blue"

        fig.add_trace(
            go.Scattermap(
                lat=subset["lat"],
                lon=subset["lon"],
                mode="lines",
                line=dict(width=2, color=color),
                name=f"{type_} - linha",
                legendgroup=type_,
                showlegend=False,
            )
        )


def _update_layout(
    fig: go.Figure,
    center_lat: float,
    center_lon: float,
    zoom: float,
) -> None:
    """Update figure layout with map configuration."""
    fig.update_layout(
        mapbox_style="open-street-map",
        mapbox_center={"lat": center_lat, "lon": center_lon},
        mapbox_zoom=zoom,
        margin=dict(l=0, r=0, t=80, b=0),
    )


def _calculate_visibility_states(
    show_original_line: bool,
) -> dict[str, list[bool]]:
    """Calculate visibility states for all button configurations."""
    # Traces: [original_points, calculated_points, original_line?, calculated_line]
    n_point_traces = 2
    n_line_traces = 2 if show_original_line else 1
    total_traces = n_point_traces + n_line_traces

    vis_none = [False] * total_traces
    vis_both = [True] * total_traces

    vis_original = [False] * total_traces
    vis_original[0] = True  # original points
    if show_original_line:
        vis_original[2] = True  # original line

    vis_calculated = [False] * total_traces
    vis_calculated[1] = True  # calculated points
    vis_calculated[-1] = True  # calculated line (last trace)

    return {
        "none": vis_none,
        "both": vis_both,
        "original": vis_original,
        "calculated": vis_calculated,
    }


def _create_toggle_buttons(
    original_label: str,
    calculated_label: str,
    visibility_states: dict[str, list[bool]],
) -> list[dict[str, Any]]:
    """Create toggle buttons configuration."""
    return [
        dict(
            type="buttons",
            direction="right",
            showactive=True,
            x=0.5,
            xanchor="center",
            y=1.02,
            yanchor="bottom",
            buttons=[
                dict(
                    label="Mostrar Nenhuma",
                    method="update",
                    args=[{"visible": visibility_states["none"]}],
                ),
                dict(
                    label="Mostrar Ambas",
                    method="update",
                    args=[{"visible": visibility_states["both"]}],
                ),
                dict(
                    label=f"Apenas {original_label}",
                    method="update",
                    args=[{"visible": visibility_states["original"]}],
                ),
                dict(
                    label=f"Apenas {calculated_label}",
                    method="update",
                    args=[{"visible": visibility_states["calculated"]}],
                ),
            ],
        )
    ]


def _add_toggle_buttons(
    fig: go.Figure,
    original_label: str,
    calculated_label: str,
    show_original_line: bool,
) -> None:
    """Add toggle buttons to figure."""
    visibility_states = _calculate_visibility_states(show_original_line)
    buttons = _create_toggle_buttons(
        original_label, calculated_label, visibility_states
    )
    fig.update_layout(updatemenus=buttons)


def _plot_on_map(
    original: list[Point],
    calculated: list[Point],
    title: str = "Tracks",
    original_label: str = "Original",
    calculated_label: str = "Calculated",
    show_original_line: bool = True,
    show_buttons: bool = True,
    center_lat: float | None = None,
    center_lon: float | None = None,
    zoom: int = 14,
) -> None:
    """
    Plot original and calculated trajectories on a map using Plotly Express.
    """
    # Prepare data
    df = _prepare_dataframe(original, calculated, original_label, calculated_label)
    center_lat, center_lon = _calculate_map_center(df, center_lat, center_lon)

    # Create figure
    fig = _create_base_figure(df, original_label, calculated_label, zoom, title)

    # Add line traces
    _add_line_traces(fig, df, original_label, calculated_label, show_original_line)

    # Update layout
    _update_layout(fig, center_lat, center_lon, zoom)

    # Add toggle buttons if requested
    if show_buttons:
        _add_toggle_buttons(fig, original_label, calculated_label, show_original_line)

    fig.show()

#!/usr/bin/env python3
"""
Maritime voyage planner between ports.

This script:
    - Loads a port database from a GeoJSON file (ports.geojson)
    - Loads a distance database (nautical miles) from a JSON file (distances.json)
    - Loads/initializes a ship database (memory file ships.json)
    - Builds a route (an ordered list of ports + a ship) and computes its
      total distance, fuel/CO2eq consumption, duration and CII score
    - Saves the route to a memory file (voyages.json) for later reuse
    - On startup, displays the routes already stored in memory, then the
      newly created route

The script is structured to be easily extended (e.g. adding other
calculations, other ship categories, etc.):
see the Route class methods, "extension point" section.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

PORTS_FILE = Path("ports.geojson")
DISTANCES_FILE = Path("distances.json")
ROUTES_MEMORY_FILE = Path("voyages.json")  # memory file #1: completed voyages
SHIPS_MEMORY_FILE = Path("ships.json")     # memory file #2: ship categories

# Default ship categories, used to initialize ships.json if it does not
# exist yet. Freely editable / extendable.
DEFAULT_SHIPS = {
    "Handymax": {
        "co2eq_kg_per_nm": 231.6,
        "dwt_tonnes": 40799,
        "avg_speed_kn": 10.5,
    },
    "Panamax": {
        "co2eq_kg_per_nm": 260.14,
        "dwt_tonnes": 52713,
        "avg_speed_kn": 9.4,
    },
    "Kamsarmax": {
        "co2eq_kg_per_nm": 305.86,
        "dwt_tonnes": 63073,
        "avg_speed_kn": 11.1,
    },
    "Capesize": {
        "co2eq_kg_per_nm": 1083.86,
        "dwt_tonnes": 220766,
        "avg_speed_kn": 15.8,
    },
}


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #

class PortDatabase:
    """Loads and gives access to port information (GeoJSON file)."""

    def __init__(self, path: Path = PORTS_FILE):
        self.path = path
        self._ports: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Supports both {"type": "FeatureCollection", "features": [...]}
        # and a plain list of features.
        features = data.get("features", data) if isinstance(data, dict) else data

        for feature in features:
            props = feature["properties"]
            locode = props["LOCODE"]
            coords = feature["geometry"]["coordinates"]
            self._ports[locode] = {
                "locode": locode,
                "name": props.get("Name"),
                "country": props.get("Country"),
                "longitude": coords[0],
                "latitude": coords[1],
                "raw": feature,
            }

    def get(self, locode: str) -> dict:
        try:
            return self._ports[locode]
        except KeyError:
            raise ValueError(f"Unknown port: {locode}") from None

    def exists(self, locode: str) -> bool:
        return locode in self._ports


class DistanceDatabase:
    """Loads and gives access to distances between ports (JSON file)."""

    def __init__(self, path: Path = DISTANCES_FILE):
        self.path = path
        self._distances: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._distances = data.get("distances", data)

    def get(self, port_a: str, port_b: str) -> float:
        """Returns the distance (in nautical miles) between two ports.

        Tries both directions (A-B then B-A), since the file does not
        necessarily contain both directions for every pair.
        """
        key_ab = f"{port_a}-{port_b}"
        key_ba = f"{port_b}-{port_a}"

        if key_ab in self._distances:
            return self._distances[key_ab]
        if key_ba in self._distances:
            return self._distances[key_ba]

        raise ValueError(f"Unknown distance between {port_a} and {port_b}")


class ShipDatabase:
    """Loads and gives access to ship categories (memory file ships.json).

    If the file does not exist yet, it is created and initialized with the
    default categories (DEFAULT_SHIPS).
    """

    def __init__(self, path: Path = SHIPS_MEMORY_FILE):
        self.path = path
        self._ships: dict[str, dict] = {}
        self._load_or_init()

    def _load_or_init(self) -> None:
        if not self.path.exists():
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"ships": DEFAULT_SHIPS}, f, ensure_ascii=False, indent=2)

        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._ships = data.get("ships", data)

    def get(self, name: str) -> dict:
        try:
            return self._ships[name]
        except KeyError:
            raise ValueError(f"Unknown ship: {name}") from None

    def exists(self, name: str) -> bool:
        return name in self._ships

    def names(self) -> list[str]:
        return list(self._ships.keys())


# --------------------------------------------------------------------------- #
# Route modeling
# --------------------------------------------------------------------------- #

def CII_score(Emission: float, DWT: float, Distance: float) -> float:
    """Carbon Intensity Indicator (CII): emissions relative to carrying
    capacity (DWT) and distance traveled."""
    return (Emission) / (DWT * Distance)


@dataclass
class Leg:
    """One step of the route, between two ports."""
    origin: str
    destination: str
    distance_nm: float


@dataclass
class Route:
    """A route made of a sequence of ports, operated by a ship."""
    ports: list[str] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    name: Optional[str] = None
    ship_name: Optional[str] = None
    ship_specs: Optional[dict] = None  # copy of the ship's specs at the time of the route

    @property
    def total_distance_nm(self) -> float:
        return sum(leg.distance_nm for leg in self.legs)

    # ------------------------------------------------------------------ #
    # EXTENSION POINT: add other calculations here (cost, port calls,
    # emissions under another standard, etc.) without touching the rest
    # of the code.
    # ------------------------------------------------------------------ #
    def total_co2eq_kg(self) -> Optional[float]:
        """Total emissions (kg CO2eq), based on the ship assigned to the route."""
        if not self.ship_specs:
            return None
        return self.total_distance_nm * self.ship_specs["co2eq_kg_per_nm"]

    def cii_score(self) -> Optional[float]:
        """Route's CII (Carbon Intensity Indicator), based on the assigned ship."""
        if not self.ship_specs:
            return None
        emission = self.total_co2eq_kg()
        dwt = self.ship_specs["dwt_tonnes"]
        return CII_score(emission, dwt, self.total_distance_nm)

    def total_duration_hours(self, speed_knots: Optional[float] = None) -> Optional[float]:
        """Total route duration. Uses the assigned ship's speed by default,
        or an explicitly provided speed."""
        speed = speed_knots if speed_knots is not None else (
            self.ship_specs["avg_speed_kn"] if self.ship_specs else None
        )
        if not speed:
            return None
        if speed <= 0:
            raise ValueError("Speed must be positive")
        return self.total_distance_nm / speed

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ports": self.ports,
            "legs": [
                {
                    "origin": leg.origin,
                    "destination": leg.destination,
                    "distance_nm": leg.distance_nm,
                }
                for leg in self.legs
            ],
            "total_distance_nm": self.total_distance_nm,
            "ship_name": self.ship_name,
            "ship_specs": self.ship_specs,
            "total_co2eq_kg": self.total_co2eq_kg(),
            "total_duration_hours": self.total_duration_hours(),
            "cii_score": self.cii_score(),
        }


def build_route(
    port_locodes: list[str],
    port_db: PortDatabase,
    distance_db: DistanceDatabase,
    ship_db: ShipDatabase,
    ship_name: Optional[str] = None,
    name: Optional[str] = None,
) -> Route:
    """Builds a route from an ordered list of LOCODEs and, optionally, a
    ship category."""
    if len(port_locodes) < 2:
        raise ValueError("A route needs at least two ports")

    for locode in port_locodes:
        if not port_db.exists(locode):
            raise ValueError(f"Unknown port in the port database: {locode}")

    legs = []
    for origin, destination in zip(port_locodes, port_locodes[1:]):
        distance = distance_db.get(origin, destination)
        legs.append(Leg(origin=origin, destination=destination, distance_nm=distance))

    ship_specs = None
    if ship_name is not None:
        if not ship_db.exists(ship_name):
            raise ValueError(f"Unknown ship: {ship_name}")
        ship_specs = ship_db.get(ship_name)

    return Route(
        ports=port_locodes,
        legs=legs,
        name=name,
        ship_name=ship_name,
        ship_specs=ship_specs,
    )


# --------------------------------------------------------------------------- #
# Saving / loading routes (memory file #1)
# --------------------------------------------------------------------------- #

def save_route(route: Route, memory_file: Path = ROUTES_MEMORY_FILE) -> None:
    """Appends the route to the memory file (created if it does not exist yet)."""
    if memory_file.exists():
        with open(memory_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"routes": []}

    data["routes"].append(route.to_dict())

    with open(memory_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_routes(memory_file: Path = ROUTES_MEMORY_FILE) -> list[dict]:
    """Reloads all previously saved routes."""
    if not memory_file.exists():
        return []
    with open(memory_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("routes", [])


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #

def display_route(route: Route, port_db: PortDatabase) -> None:
    title = route.name or "Route"
    print(f"=== {title} ===")
    for i, locode in enumerate(route.ports):
        port = port_db.get(locode)
        print(f"  {i + 1}. {port['name']} ({locode}) - {port['country']}")
        if i < len(route.legs):
            leg = route.legs[i]
            print(f"       -> {leg.distance_nm:.1f} NM ->")
    print(f"Total distance: {route.total_distance_nm:.1f} nautical miles")

    if route.ship_name:
        print(f"Ship: {route.ship_name}")
        co2 = route.total_co2eq_kg()
        duration = route.total_duration_hours()
        if co2 is not None:
            print(f"Estimated emissions: {co2:.1f} kg CO2eq")
        if duration is not None:
            print(f"Estimated duration: {duration:.1f} h")
        cii = route.cii_score()
        if cii is not None:
            print(f"CII score: {cii:.6f}")
    print()


def display_saved_routes(memory_file: Path = ROUTES_MEMORY_FILE) -> None:
    """On startup, displays the routes already stored in memory and their
    total distance."""
    routes = load_routes(memory_file)

    print("=== Routes in memory ===")
    if not routes:
        print("  (no route saved yet)")
    else:
        for i, route_data in enumerate(routes, start=1):
            title = route_data.get("name") or f"Route {i}"
            ports = " -> ".join(route_data.get("ports", []))
            distance = route_data.get("total_distance_nm", 0.0)
            ship = route_data.get("ship_name")
            print(f"  {i}. {title}: {ports}")
            print(f"     Total distance: {distance:.1f} NM" + (f" | Ship: {ship}" if ship else ""))
    print()


def display_ships(ship_db: ShipDatabase) -> None:
    """Displays the available ship categories."""
    print("=== Available ships ===")
    for name in ship_db.names():
        specs = ship_db.get(name)
        print(
            f"  - {name}: {specs['avg_speed_kn']} kn | "
            f"DWT {specs['dwt_tonnes']} T | "
            f"{specs['co2eq_kg_per_nm']} kg CO2eq/NM"
        )
    print()


# --------------------------------------------------------------------------- #
# Interactive input
# --------------------------------------------------------------------------- #

def prompt_route_ports(port_db: PortDatabase) -> list[str]:
    """Asks the user for the number of ports, then each LOCODE, re-prompting
    as long as an unknown port is entered."""
    while True:
        try:
            nb_ports = int(input("Number of ports in the route: ").strip())
            if nb_ports < 2:
                print("A route needs at least 2 ports.")
                continue
            break
        except ValueError:
            print("Please enter an integer.")

    ports: list[str] = []
    for i in range(1, nb_ports + 1):
        while True:
            locode = input(f"Port {i}/{nb_ports} (LOCODE, e.g. BEANR): ").strip().upper()
            if port_db.exists(locode):
                ports.append(locode)
                break
            print(f"  Unknown port: '{locode}'. Try again.")

    return ports


def prompt_ship(ship_db: ShipDatabase) -> Optional[str]:
    """Asks the user to choose a ship among the available categories."""
    names = ship_db.names()
    print("Available ships:")
    for i, name in enumerate(names, start=1):
        print(f"  {i}. {name}")

    while True:
        choice = input("Ship choice (number, or empty for none): ").strip()
        if choice == "":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
        print("Invalid choice, try again.")


# --------------------------------------------------------------------------- #
# Main program
# --------------------------------------------------------------------------- #

def main() -> None:
    port_db = PortDatabase()
    distance_db = DistanceDatabase()
    ship_db = ShipDatabase()

    # Show the state of both memory files on startup
    display_saved_routes()
    display_ships(ship_db)

    # Interactive input for the new route
    route_ports = prompt_route_ports(port_db)
    ship_name = prompt_ship(ship_db)
    route_name = input("Route name (optional): ").strip() or None

    route = build_route(
        route_ports, port_db, distance_db, ship_db,
        ship_name=ship_name, name=route_name,
    )

    display_route(route, port_db)
    save_route(route)
    print(f"Route saved to '{ROUTES_MEMORY_FILE}'")


if __name__ == "__main__":
    main()
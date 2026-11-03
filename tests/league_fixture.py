"""Ein kleines Match im Format von /valorant/v4/match für die Liga-Tests (drei Runden, Rot gegen Blau).

Runde 1 (Pistol): Zoe holt den First Kill, stirbt und wird von Kai getradet; Kai macht 3 Kills, Rot gewinnt.
Runde 2: Blau gewinnt, B1 macht 4 Kills, R5 bleibt allein, pflanzt und verliert; B4 entschärft.
Runde 3: R5 gewinnt den 1v5-Clutch.
"""

RED = ["zoe", "kai", "r3", "r4", "r5"]
BLUE = ["b1", "b2", "b3", "b4", "b5"]
NAMES = {"zoe": ("Zoe", "EUW"), "kai": ("Kai", "EUW")}


def player(puuid: str, team: str, score: int, kills: int, deaths: int, assists: int = 0) -> dict:
    name, tag = NAMES.get(puuid, (puuid.upper(), "0000"))
    return {
        "puuid": puuid, "name": name, "tag": tag, "team_id": team, "platform": "pc", "party_id": "p",
        "agent": {"id": "a", "name": "Jett" if team == "Red" else "Sova"},
        "stats": {"score": score, "kills": kills, "deaths": deaths, "assists": assists, "headshots": 10,
                  "bodyshots": 30, "legshots": 0, "damage": {"dealt": 3 * score // 2, "received": 300}},
    }


def kill(round_id: int, time: int, killer: str, victim: str, assistants=()) -> dict:
    team = lambda puuid: "Red" if puuid in RED else "Blue"
    return {
        "round": round_id, "time_in_round_in_ms": time,
        "killer": {"puuid": killer, "name": killer, "tag": "x", "team": team(killer)},
        "victim": {"puuid": victim, "name": victim, "tag": "x", "team": team(victim)},
        "assistants": [{"puuid": puuid, "name": puuid, "tag": "x", "team": team(puuid)} for puuid in assistants],
        "weapon": {"id": "w", "name": "Vandal", "type": "Weapon"},
    }


def round_entry(round_id: int, winner: str, result: str, red_loadout: int, blue_loadout: int,
                ceremony: str = "CeremonyDefault", plant=None, defuse=None) -> dict:
    stats = [{"player": {"puuid": puuid}, "economy": {"loadout_value": red_loadout, "remaining": 0}} for puuid in RED]
    stats += [{"player": {"puuid": puuid}, "economy": {"loadout_value": blue_loadout, "remaining": 0}} for puuid in BLUE]
    return {
        "id": round_id, "winning_team": winner, "result": result, "ceremony": ceremony, "stats": stats,
        "plant": {"player": {"puuid": plant[0]}, "site": plant[1], "round_time_in_ms": 30000} if plant else None,
        "defuse": {"player": {"puuid": defuse}, "round_time_in_ms": 60000} if defuse else None,
    }


def v4_match(match_id: str = "11111111-2222-3333-4444-555555555555", map_name: str = "Ascent",
             started_at: str = "2026-10-28T18:00:00.000Z") -> dict:
    return {
        "metadata": {
            "match_id": match_id, "map": {"id": "m", "name": map_name}, "game_length_in_ms": 1_800_000,
            "started_at": started_at, "is_completed": True, "queue": {"id": "custom", "name": "Custom"},
        },
        "players": [
            player("zoe", "Red", 300, 1, 3), player("kai", "Red", 280, 3, 1, 1), player("r3", "Red", 100, 0, 2, 1),
            player("r4", "Red", 150, 1, 2), player("r5", "Red", 400, 7, 1),
            player("b1", "Blue", 500, 8, 2), player("b2", "Blue", 120, 1, 2), player("b3", "Blue", 110, 1, 2),
            player("b4", "Blue", 90, 0, 2), player("b5", "Blue", 80, 0, 2),
        ],
        "teams": [
            {"team_id": "Red", "rounds": {"won": 2, "lost": 1}, "won": True},
            {"team_id": "Blue", "rounds": {"won": 1, "lost": 2}, "won": False},
        ],
        "rounds": [
            round_entry(0, "Red", "Eliminated", 800, 800),
            round_entry(1, "Blue", "Bomb defused", 900, 4500, plant=("r5", "A"), defuse="b4"),
            round_entry(2, "Red", "Eliminated", 5000, 5000, ceremony="CeremonyClutch"),
        ],
        "kills": [
            kill(0, 10000, "zoe", "b1"), kill(0, 12000, "b2", "zoe"), kill(0, 14000, "kai", "b2", ["r3"]),
            kill(0, 20000, "kai", "b3"), kill(0, 25000, "kai", "b4"), kill(0, 30000, "r4", "b5"),
            kill(1, 5000, "b1", "zoe"), kill(1, 6000, "b1", "kai"), kill(1, 7000, "b1", "r3"), kill(1, 8000, "b1", "r4"),
            kill(1, 9000, "r5", "b1"), kill(1, 10000, "r5", "b2"), kill(1, 11000, "b3", "r5"),
            kill(2, 1000, "b1", "zoe"), kill(2, 2000, "b1", "kai"), kill(2, 3000, "b1", "r3"), kill(2, 4000, "b1", "r4"),
            kill(2, 5000, "r5", "b1"), kill(2, 6000, "r5", "b2"), kill(2, 7000, "r5", "b3"), kill(2, 8000, "r5", "b4"),
            kill(2, 9000, "r5", "b5"),
        ],
    }


RIOT_IDS = {"zoe#euw": 5, "kai#euw": 6}

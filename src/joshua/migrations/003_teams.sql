-- Team games: "Teamplay" or "Regular", and each player's team when WarGear shows it.
-- Teammates never attack each other, so the after-action report mustn't call them allies,
-- and a team win counts for the whole team.
ALTER TABLE games ADD COLUMN teamplay TEXT;
ALTER TABLE game_players ADD COLUMN team TEXT;

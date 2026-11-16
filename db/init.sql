-- Example source schema for pg-subset-exporter.
-- 6 plain tables in schema public; integer/text columns only, no defaults;
-- single-column integer primary keys; unique + foreign key constraints.
-- Covers: circular reference (users <-> teams), self reference (tasks),
-- nullable composite foreign key (attachments -> task_links), NULLs.

CREATE TABLE teams (
    id   integer PRIMARY KEY,
    name text NOT NULL UNIQUE,
    lead_id integer                 -- FK added below (circular with users)
);

CREATE TABLE users (
    id    integer PRIMARY KEY,
    name  text NOT NULL,
    email text UNIQUE,
    team_id integer                 -- FK added below (circular with teams)
);

ALTER TABLE teams ADD CONSTRAINT teams_lead_fk
    FOREIGN KEY (lead_id) REFERENCES users (id);
ALTER TABLE users ADD CONSTRAINT users_team_fk
    FOREIGN KEY (team_id) REFERENCES teams (id);

CREATE TABLE projects (
    id       integer PRIMARY KEY,
    name     text NOT NULL,
    owner_id integer NOT NULL,
    CONSTRAINT projects_owner_fk FOREIGN KEY (owner_id) REFERENCES users (id),
    CONSTRAINT projects_owner_name_key UNIQUE (owner_id, name)
);

CREATE TABLE tasks (
    id          integer PRIMARY KEY,
    title       text NOT NULL,
    project_id  integer NOT NULL,
    assignee_id integer,
    parent_id   integer,
    CONSTRAINT tasks_project_fk  FOREIGN KEY (project_id)  REFERENCES projects (id),
    CONSTRAINT tasks_assignee_fk FOREIGN KEY (assignee_id) REFERENCES users (id),
    CONSTRAINT tasks_parent_fk   FOREIGN KEY (parent_id)   REFERENCES tasks (id)  -- self reference
);

CREATE TABLE task_links (
    id      integer PRIMARY KEY,
    task_id integer NOT NULL,
    label   text NOT NULL,
    CONSTRAINT task_links_task_fk FOREIGN KEY (task_id) REFERENCES tasks (id),
    CONSTRAINT task_links_task_label_key UNIQUE (task_id, label)
);

CREATE TABLE attachments (
    id        integer PRIMARY KEY,
    file_name text NOT NULL,
    task_id   integer,
    label     text,
    CONSTRAINT attachments_task_fk
        FOREIGN KEY (task_id, label)
        REFERENCES task_links (task_id, label)      -- nullable composite FK
);

-- Used explicitly (nextval) by the writer service; columns keep no defaults.
CREATE SEQUENCE writer_seq START 100000;

-- ---------------------------------------------------------------- seed data
INSERT INTO teams (id, name, lead_id) VALUES
    (1, 'core', NULL),
    (2, 'research', NULL);

INSERT INTO users (id, name, email, team_id) VALUES
    (1, 'alice', 'alice@example.com', 1),
    (2, 'bob', NULL, 1),
    (3, 'carol', 'carol@example.com', 2),
    (4, 'dave', NULL, 2),
    (5, 'erin', 'erin@example.com', NULL);

UPDATE teams SET lead_id = 2 WHERE id = 1;
UPDATE teams SET lead_id = 4 WHERE id = 2;

INSERT INTO projects (id, name, owner_id) VALUES
    (10, 'website', 1),
    (11, 'api', 1),
    (12, 'ml-model', 3);

INSERT INTO tasks (id, title, project_id, assignee_id, parent_id) VALUES
    (100, 'design', 10, 2, NULL),
    (101, 'build', 10, 2, 100),
    (102, 'te''st "quoted" 中文', 11, NULL, 101),
    (103, 'train', 12, 4, NULL);

INSERT INTO task_links (id, task_id, label) VALUES
    (1000, 100, 'spec'),
    (1001, 100, 'mock'),
    (1002, 101, 'spec');

INSERT INTO attachments (id, file_name, task_id, label) VALUES
    (5000, 'spec.pdf', 100, 'spec'),
    (5001, 'notes.txt', 100, NULL),      -- NULL part: FK not enforced (MATCH SIMPLE)
    (5002, 'mock.png', 100, 'mock');

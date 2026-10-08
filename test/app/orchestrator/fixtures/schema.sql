-- Production DDL (pg_dump --schema-only of agents, users, tasks, jobs, sessions on ai_pin_db, 2026-10-07)
-- plus the tasks_touch_updated_at trigger function, which pg_dump -t omits. Test fixture only.
CREATE OR REPLACE FUNCTION public.tasks_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.updated_at := now(); RETURN NEW; END $$;
--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agents (
    agent_id uuid NOT NULL,
    agent_info jsonb,
    agent_url text
);


--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jobs (
    id bigint NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    deliver_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    done_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL
);


--
-- Name: jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.jobs_id_seq OWNED BY public.jobs.id;


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sessions (
    user_id uuid NOT NULL,
    scratchpad text,
    is_active boolean NOT NULL
);


--
-- Name: tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tasks (
    task_id uuid NOT NULL,
    user_id uuid NOT NULL,
    task_info jsonb,
    status text DEFAULT 'pending'::text NOT NULL,
    time_to_execute timestamp with time zone,
    enqueue_sequence_id bigint,
    is_scheduled boolean GENERATED ALWAYS AS ((time_to_execute IS NOT NULL)) STORED,
    kind text DEFAULT 'reminder'::text NOT NULL,
    created_by text,
    agent_id uuid,
    notify text DEFAULT 'device'::text NOT NULL,
    question text,
    result jsonb,
    deadline_at timestamp with time zone,
    finished_at timestamp with time zone,
    delivered_at timestamp with time zone,
    delivered_via text,
    agent_informed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tasks_agent_task_has_agent CHECK (((kind <> 'agent_task'::text) OR (agent_id IS NOT NULL))),
    CONSTRAINT tasks_kind_check CHECK ((kind = ANY (ARRAY['reminder'::text, 'agent_task'::text]))),
    CONSTRAINT tasks_notify_check CHECK ((notify = ANY (ARRAY['device'::text, 'next_session'::text, 'silent'::text]))),
    CONSTRAINT tasks_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'dispatching'::text, 'running'::text, 'input_required'::text, 'completed'::text, 'failed'::text, 'cancelled'::text, 'timed_out'::text])))
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    user_id uuid NOT NULL,
    first_name text,
    last_name text,
    firebase_uid text,
    username text,
    timezone text DEFAULT 'UTC'::text NOT NULL,
    device_prefix text
);


--
-- Name: jobs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs ALTER COLUMN id SET DEFAULT nextval('public.jobs_id_seq'::regclass);


--
-- Name: tasks Tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT "Tasks_pkey" PRIMARY KEY (task_id);


--
-- Name: users Users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT "Users_pkey" PRIMARY KEY (user_id);


--
-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (agent_id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (user_id);


--
-- Name: agents_name_lower_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agents_name_lower_idx ON public.agents USING btree (lower((agent_info ->> 'name'::text)));


--
-- Name: agents_service_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agents_service_id_idx ON public.agents USING btree (((agent_info ->> 'service_id'::text)));


--
-- Name: jobs_pending_deliver_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX jobs_pending_deliver_at_idx ON public.jobs USING btree (deliver_at) WHERE (done_at IS NULL);


--
-- Name: tasks_agent_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tasks_agent_idx ON public.tasks USING btree (agent_id) WHERE (agent_id IS NOT NULL);


--
-- Name: tasks_enqueue_seq_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tasks_enqueue_seq_idx ON public.tasks USING btree (enqueue_sequence_id) WHERE (enqueue_sequence_id IS NOT NULL);


--
-- Name: tasks_undelivered_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tasks_undelivered_idx ON public.tasks USING btree (user_id) WHERE ((kind = 'agent_task'::text) AND (delivered_at IS NULL) AND (status = ANY (ARRAY['completed'::text, 'failed'::text, 'timed_out'::text])));


--
-- Name: tasks_user_scheduled_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tasks_user_scheduled_idx ON public.tasks USING btree (user_id, time_to_execute) WHERE (time_to_execute IS NOT NULL);


--
-- Name: tasks_user_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tasks_user_status_idx ON public.tasks USING btree (user_id, status);


--
-- Name: tasks tasks_touch_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER tasks_touch_updated_at BEFORE UPDATE ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.tasks_touch_updated_at();


--
-- Name: sessions sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE;


--
-- Name: tasks tasks_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id) ON DELETE SET NULL;


--
-- Name: tasks tasks_enqueue_sequence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_enqueue_sequence_id_fkey FOREIGN KEY (enqueue_sequence_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: tasks tasks_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--



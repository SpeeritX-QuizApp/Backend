import asyncio
import json
import random

from asgiref.sync import sync_to_async
from channels.consumer import SyncConsumer, AsyncConsumer
from channels.db import database_sync_to_async

from game.models import Question
from channels.generic.websocket import AsyncWebsocketConsumer, WebsocketConsumer


""" GameWorker - manages all active games """


class GameWorker(AsyncConsumer):
    QUESTION_LENGTH = 10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_games = {}
        self.current_players = {}  # {'player_channel_name': 'game_code', ...}
        self.current_question = None
        print('GameWorker started.')

    def _get_new_game_code(self):
        while True:
            code = random.randint(0, 99)
            if not self.active_games.get(code, None):
                return str(code)

    async def _remove_player_from_game(self, channel_name):
        self.active_games[self.current_players[channel_name]].remove_user(channel_name)
        await self.channel_layer.group_discard(self.current_players[channel_name], channel_name)
        del self.current_players[channel_name]

    async def _add_player_to_game(self, channel_name, username, game_code):
        new_username = self.active_games[game_code].add_user(channel_name, username)
        self.current_players[channel_name] = game_code
        await self.channel_layer.group_add(game_code, channel_name)
        return new_username

    async def _remove_game(self, game_code):
        players = self.active_games[game_code].get_list_of_users_channels()
        for p in players:
            await self._remove_player_from_game(p)
        self.active_games.pop(game_code)

    async def _send_error(self, channel_name, msg):
        await self.channel_layer.send(
            channel_name,
            {
                "type": "error",
                "msg": msg,
            },
        )

    async def _ask_question(self, game_code, question, question_id):
        self.current_question = {'id': question_id, 'correct_answer': question.correct_answer}

        await self.channel_layer.group_send(
            game_code,
            {
                'type': 'ask_question',
                'question_id': question_id,
                'time': self.QUESTION_LENGTH,
                'question': question.content,
                'answers': question.answers
            }
        )

        await asyncio.sleep(self.QUESTION_LENGTH)

    async def _get_random_questions(self, amount):
        questions = await database_sync_to_async(Question.objects.all().order_by)('?')
        random_questions = await sync_to_async(list)(questions[:amount])
        return random_questions

    async def _run_game(self, game_code):
        await self.channel_layer.group_send(
            game_code,
            {
                'type': 'game_started',
            }
        )

        questions = await self._get_random_questions(3)

        for i, q in enumerate(questions):
            await self._ask_question(game_code, q, i)

        await self.channel_layer.group_send(
            game_code,
            {
                'type': 'game_ended',
                'scores': self.active_games[game_code].get_all_scores()
            }
        )
        await self._remove_game(game_code)

    async def create_game(self, event):
        username = event['username']
        channel_name = event['channel_name']
        game_code = self._get_new_game_code()

        if not username:
            await self._send_error(channel_name, "Some data is missing")
            return

        if channel_name in self.current_players:
            await self._remove_player_from_game(channel_name)

        self.active_games[game_code] = GameState(game_code)
        await self._add_player_to_game(channel_name, username, game_code)
        await self.channel_layer.send(
            channel_name,
            {
                "type": "game_created",
                "game_code": game_code,
            },
        )

    async def add_user(self, event):
        channel_name = event['channel_name']
        username = event['username']
        game_code = event['game_code']

        if not username or game_code is None:
            await self._send_error(channel_name, "Some data is missing")
            return

        if channel_name in self.current_players:
            if self.current_players[channel_name] == game_code:
                await self._send_error(channel_name, 'You are in this game already')
                return
            await self._remove_player_from_game(channel_name)

        if game_code not in self.active_games:  # If game does not exist
            await self._send_error(channel_name, f"Game with code {game_code} does not exist")
            return
        elif self.active_games[game_code].is_game_running():
            await self._send_error(channel_name, f"Game with code {game_code} is running already")
            return

        new_username = await self._add_player_to_game(channel_name, username, game_code)

        await self.channel_layer.send(
            channel_name,
            {
                "type": "join_successful",
                "username": new_username,
            },
        )

        users = self.active_games[game_code].get_list_of_usernames()
        await self.channel_layer.group_send(
            game_code,
            {
                "type": "show_users",
                "users": users,
            },
        )

    async def remove_user(self, event):
        channel_name = event['channel_name']
        if channel_name not in self.current_players:
            await self._send_error(channel_name, "You are not in a game")
            return
        await self._remove_player_from_game(channel_name)

    async def submit_answer(self, event):
        channel_name = event['channel_name']

        if channel_name not in self.current_players:
            await self._send_error(channel_name, "You are not in a game")
            return

        try:
            question_id = int(event['question_id'])
        except Exception as e:
            await self._send_error(channel_name, "Wrong question id")
            return

        if question_id != self.current_question['id']:
            if self.current_question['id'] > question_id >= 0:
                await self._send_error(channel_name, "Question already ended")
                return
            else:
                await self._send_error(channel_name, "Wrong question id")
                return

        try:
            answer = int(event['answer'])
        except Exception as e:
            await self._send_error(channel_name, "Wrong answer format")
            return

        score = 0
        if answer == self.current_question['correct_answer']:
            score = 10
        game_code = self.current_players[channel_name]
        if self.active_games[game_code].set_answer(channel_name, question_id, score):  # If first try, send result
            await self.channel_layer.send(
                channel_name,
                {
                    'type': 'question_end',
                    'question_id': self.current_question['id'],
                    'correct_answer': True if score > 0 else False,
                }
            )

    async def start_game(self, event):
        channel_name = event['channel_name']
        if channel_name not in self.current_players:
            await self._send_error(channel_name, "You are not in a game")
            return

        game_code = self.current_players[channel_name]
        if self.active_games[game_code].start_game():  # If starting game was successful (game is not running yet)
            asyncio.create_task(self._run_game(game_code))
        else:
            await self._send_error(channel_name, "Game is running already")


class GameState:
    def __init__(self, code):
        self.code = code
        self.users = []
        self.running = False

    def start_game(self):
        if self.running:
            return False
        self.running = True
        return True

    def is_game_running(self):
        return self.running

    def is_username_available(self, username):
        for u in self.users:
            if u["username"] == username:
                return False
        return True

    def add_user(self, channel_name, username):
        if not self.is_username_available(username):
            index = 2
            while not self.is_username_available(username + f" #{index}"):
                index += 1
            username = username + f" #{index}"
        self.users.append({"channel_name": channel_name, 'username': username, "answers": []})
        return username

    def remove_user(self, channel_name):
        self.users.remove(self.get_user(channel_name))
        return

    def get_user(self, channel_name):
        for u in self.users:
            if u["channel_name"] == channel_name:
                return u
        return None

    def count_user_score(self, user):
        score = 0
        for q in user['answers']:
            score += q['score']
        return score

    def set_answer(self, channel_name, question_id, score):
        user = self.get_user(channel_name)
        for q in user['answers']:
            if q['question_id'] == question_id:
                return False
        user['answers'].append({"question_id": question_id, "score": score})
        return True

    def check_if_user_was_right(self, channel_name, question_id):
        user = self.get_user(channel_name)
        for q in user['answers']:
            if q['question_id'] == question_id:
                if q['score'] > 0:
                    return True
                return False
        return False

    def get_all_scores(self):
        return [{'user': u['username'], 'score': self.count_user_score(u)} for u in self.users]

    def get_list_of_usernames(self):
        return [u["username"] for u in self.users]

    def get_list_of_users_channels(self):
        return [u["channel_name"] for u in self.users]

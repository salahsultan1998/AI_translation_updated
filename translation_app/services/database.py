"""Per-user Supabase access; never share mutable authentication between users."""
import os


class ProjectDatabase:
    def __init__(self, access_token, *, allow_anonymous=False, storage_root=None):
        from supabase import create_client
        url, key = os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY')
        if not url or not key or (not access_token and not allow_anonymous):
            raise ValueError('数据库或登录信息未配置')
        self.client = create_client(url, key)
        self.storage_root = storage_root
        if access_token:
            self.client.postgrest.auth(access_token)

    def get_game_profile(self, project_id):
        response = (self.client.table('game_profiles').select('tone_style')
                    .eq('game_id', project_id.lower().strip()).limit(1).execute())
        return response.data[0]['tone_style'] if response.data else ''

    def upsert_game_profile(self, project_id, profile):
        from .resources import invalidate_resources
        try:
            self.client.table('game_profiles').upsert(
                {'game_id': project_id.lower().strip(), 'tone_style': profile.strip()},
                on_conflict='game_id').execute()
        finally:
            invalidate_resources(project_id, storage_root=self.storage_root)

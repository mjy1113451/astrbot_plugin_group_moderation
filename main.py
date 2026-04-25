import aiohttp
import base64
import json
import re
import time
from collections import defaultdict
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image


class ImageModerationPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.spam_records = defaultdict(list)
        self.violation_stats = defaultdict(lambda: {"image": 0, "spam": 0, "profanity": 0, "ad": 0, "link": 0})
        logger.info(f"[群违规检测] 配置加载完成")

    async def initialize(self):
        logger.info("[群违规检测] 插件初始化完成")
        api_type = self.config.get("api_type", "openai_vision")
        logger.info(f"[群违规检测] API类型: {api_type}")
        logger.info(f"[群违规检测] API站点: {self.config.get('api_endpoint', '未配置')}")
        logger.info(f"[群违规检测] 模型: {self.config.get('model_name', 'gpt-4o')}")
        logger.info(f"[群违规检测] 监控群组: {self.config.get('enabled_groups', '全部')}")
        logger.info(f"[群违规检测] 图片违规禁言: {self.config.get('ban_duration', 600)} 秒")
        logger.info(f"[群违规检测] 刷屏禁言: {self.config.get('spam_ban_duration', 600)} 秒")
        logger.info(f"[群违规检测] 骂人禁言: {self.config.get('profanity_ban_duration', 600)} 秒")
        logger.info(f"[群违规检测] 广告禁言: {self.config.get('ad_ban_duration', 600)} 秒")
        logger.info(f"[群违规检测] 链接禁言: {self.config.get('link_ban_duration', 600)} 秒")
        logger.info(f"[群违规检测] 群号推广禁言: {self.config.get('group_promotion_ban_duration', 600)} 秒")
        logger.info(f"[群违规检测] 刷屏检测: {'启用' if self.config.get('spam_check_enabled', True) else '禁用'}")
        logger.info(f"[群违规检测] 骂人检测: {'启用' if self.config.get('profanity_check_enabled', True) else '禁用'}")
        logger.info(f"[群违规检测] 广告检测: {'启用' if self.config.get('ad_check_enabled', True) else '禁用'}")
        logger.info(f"[群违规检测] 链接检测: {'启用' if self.config.get('link_check_enabled', False) else '禁用'}")
        logger.info(f"[群违规检测] 群号推广检测: {'启用' if self.config.get('group_promotion_check_enabled', True) else '禁用'}")
        logger.info(f"[群违规检测] 白名单用户: {len(self.config.get('whitelist_users', []))} 人")
        logger.warning("[群违规检测] [警告] 重要提示：机器人需要有群管理员权限才能撤回消息和禁言用户！")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            return

        user_id = event.get_sender_id()
        logger.info(f"[群违规检测] 收到群消息 - 群:{group_id} 用户:{user_id}")

        whitelist_users = self.config.get("whitelist_users", [])
        if str(user_id) in [str(u) for u in whitelist_users]:
            logger.info(f"[群违规检测] 用户 {user_id} 在白名单中，跳过检测")
            return

        if self.config.get("admin_bypass", True) and event.role == "admin":
            logger.info(f"[群违规检测] 管理员豁免，跳过检测")
            return

        enabled_groups = self.config.get("enabled_groups", [])
        if enabled_groups and str(group_id) not in [str(g) for g in enabled_groups]:
            logger.info(f"[群违规检测] 群 {group_id} 不在监控列表中，跳过")
            return

        if self.config.get("spam_check_enabled", True):
            if await self._check_spam(event, group_id, user_id):
                return

        if self.config.get("profanity_check_enabled", True):
            if await self._check_profanity(event, group_id, user_id):
                return

        if self.config.get("ad_check_enabled", True):
            if await self._check_ad(event, group_id, user_id):
                return

        if self.config.get("link_check_enabled", False):
            if await self._check_link(event, group_id, user_id):
                return

        if self.config.get("group_promotion_check_enabled", True):
            if await self._check_group_promotion(event, group_id, user_id):
                return

        messages = event.get_messages()
        image_urls = []

        for msg in messages:
            if isinstance(msg, Image):
                url = getattr(msg, "url", None) or getattr(msg, "file", None)
                if url:
                    image_urls.append(url)

        if not image_urls:
            logger.debug(f"[群违规检测] 消息中未检测到图片")
            return

        logger.info(f"[群违规检测] 检测到 {len(image_urls)} 张图片，开始审核...")

        for image_url in image_urls:
            try:
                is_violation, reason = await self._check_image(image_url)
                if is_violation:
                    await self._handle_image_violation(event, reason)
                    return
            except Exception as e:
                logger.error(f"[群违规检测] 检测图片失败: {e}")

    async def _check_image(self, image_url: str) -> tuple[bool, str]:
        api_endpoint = self.config.get("api_endpoint", "")
        api_key = self.config.get("api_key", "")
        api_type = self.config.get("api_type", "openai_vision")

        if not api_endpoint:
            logger.warning("[群违规检测] API站点未配置，请前往管理面板配置")
            return False, ""

        logger.info(f"[群违规检测] 开始下载图片...")

        try:
            image_data = await self._download_image(image_url)
            if not image_data:
                logger.error("[群违规检测] 图片下载失败")
                return False, ""

            logger.info(f"[群违规检测] 图片下载成功，大小: {len(image_data)} bytes")

            image_base64 = base64.b64encode(image_data).decode("utf-8")

            logger.info(f"[群违规检测] 调用 API 进行审核... API类型: {api_type}")

            if api_type == "openai_vision":
                return await self._check_with_openai_vision(api_endpoint, api_key, image_base64)
            else:
                return await self._check_with_moderation_api(api_endpoint, api_key, image_base64)

        except aiohttp.ClientError as e:
            logger.error(f"[群违规检测] 网络请求错误: {e}")
            return False, ""
        except Exception as e:
            logger.error(f"[群违规检测] 检测过程出错: {e}")
            return False, ""

    async def _check_with_openai_vision(self, api_endpoint: str, api_key: str, image_base64: str) -> tuple[bool, str]:
        model_name = self.config.get("model_name", "gpt-4o")
        detection_prompt = self.config.get("detection_prompt", self._get_default_prompt())

        logger.info(f"[群违规检测] 使用模型: {model_name}")

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": detection_prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 500,
            "temperature": 0.1
        }

        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        logger.info(f"[群违规检测] 发送 API 请求到: {api_endpoint}")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[群违规检测] OpenAI Vision API请求失败: {response.status} - {error_text}")
                    return False, ""

                result = await response.json()
                logger.info(f"[群违规检测] API 响应成功，开始解析结果...")
                return self._parse_openai_response(result)

    async def _check_with_moderation_api(self, api_endpoint: str, api_key: str, image_base64: str) -> tuple[bool, str]:
        payload = {
            "input": image_base64
        }

        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[群违规检测] Moderation API请求失败: {response.status}")
                    return False, ""

                result = await response.json()
                return self._parse_moderation_response(result)

    def _get_default_prompt(self) -> str:
        return """你是一个极其严格的内容审核助手。请仔细分析这张图片，判断是否包含以下违规内容：

【色情内容（porn）】- 必须判定为违规：
- 任何形式的裸露（包括但不限于：胸部、臀部、生殖器的任何暴露）
- 性暗示动作或姿势
- 过度暴露的服装（比基尼、内衣、泳装等）
- 任何性感挑逗的表情或动作

【擦边内容（sexy）】- 必须判定为违规：
- 暴露的服装（低胸、短裙、露背等）
- 性感姿势或表情
- 暗示性的身体接触
- 任何可能引起不适的暴露内容

【重要判断标准】：
1. **不管什么风格**（卡通、动漫、真人等），只要暴露就是违规
2. **宁可误判也不要漏判**，保护用户安全是第一位的
3. 只要穿着暴露、姿势性感，就必须判定为违规
4. 不要被"卡通风格"、"可爱风格"迷惑，暴露就是违规

请以严格的 JSON 格式回复（不要添加任何其他文字、不要用markdown代码块）：
{"is_violation": true, "type": "porn", "confidence": 0.95, "reason": "检测到暴露内容"}

注意：
- is_violation 必须是 true 或 false（布尔值，不要加引号）
- type 只能是 "porn"、"sexy" 或 "normal" 之一
- confidence 必须是 0.0 到 1.0 之间的数字（如果确定违规，confidence 应该 >= 0.8）
- reason 简要说明违规原因
- **如果图片中有任何暴露内容，is_violation 必须为 true**"""

    def _parse_openai_response(self, result: dict) -> tuple[bool, str]:
        try:
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.info(f"[群违规检测] AI 原始响应: {content}")

            content = content.strip()
            if content.startswith("```"):
                lines = content.split('\n')
                content = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])
                logger.info(f"[群违规检测] 移除markdown代码块后: {content}")

            json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
            if json_match:
                json_str = json_match.group()
                logger.info(f"[群违规检测] 提取的 JSON: {json_str}")
                data = json.loads(json_str)
            else:
                logger.warning(f"[群违规检测] 未找到 JSON 格式，尝试直接解析")
                data = json.loads(content)

            is_violation = data.get("is_violation", False)
            violation_type = data.get("type", "normal").lower()
            confidence = float(data.get("confidence", 0))
            reason = data.get("reason", "")

            logger.info(f"[群违规检测] 解析结果 - 类型: {violation_type}, 置信度: {confidence:.0%}, 违规: {is_violation}, 原因: {reason}")

            threshold = self.config.get("threshold", 0.7)
            check_porn = self.config.get("check_porn", True)
            check_sexy = self.config.get("check_sexy", True)

            logger.info(f"[群违规检测] 判断条件 - 阈值: {threshold}, 检测色情: {check_porn}, 检测擦边: {check_sexy}")

            if is_violation and confidence >= threshold:
                if violation_type == "porn" and check_porn:
                    logger.warning(f"[群违规检测] [警告] 检测到色情内容！置信度: {confidence:.0%}")
                    return True, f"检测到色情内容 (置信度: {confidence:.0%}) - {reason}"
                elif violation_type == "sexy" and check_sexy:
                    logger.warning(f"[群违规检测] [警告] 检测到擦边内容！置信度: {confidence:.0%}")
                    return True, f"检测到擦边内容 (置信度: {confidence:.0%}) - {reason}"
                else:
                    logger.info(f"[群违规检测] 违规类型 {violation_type} 未启用检测或置信度不足")

            logger.info(f"[群违规检测] [通过] 图片审核通过，未检测到违规内容")
            return False, ""

        except json.JSONDecodeError as e:
            logger.error(f"[群违规检测] JSON 解析失败: {e}, 原始内容: {content}")
            return False, ""
        except Exception as e:
            logger.error(f"[群违规检测] 解析 AI 响应失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False, ""

    def _parse_moderation_response(self, result: dict) -> tuple[bool, str]:
        try:
            results = result.get("results", [])
            if not results:
                return False, ""

            moderation_result = results[0]
            categories = moderation_result.get("categories", {})
            category_scores = moderation_result.get("category_scores", {})

            if categories.get("sexual", False):
                confidence = category_scores.get("sexual", 0)
                return True, f"检测到性内容 (置信度: {confidence:.0%})"

            return False, ""

        except Exception as e:
            logger.error(f"[群违规检测] 解析 Moderation API 响应失败: {e}")
            return False, ""

    async def _download_image(self, url: str) -> bytes:
        try:
            if url.startswith("http://") or url.startswith("https://"):
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status == 200:
                            return await response.read()
            elif url.startswith("base64://"):
                return base64.b64decode(url[9:])
            elif url.startswith("file://"):
                with open(url[7:], "rb") as f:
                    return f.read()
            else:
                with open(url, "rb") as f:
                    return f.read()
        except Exception as e:
            logger.error(f"[群违规检测] 下载图片失败: {e}")
        return None

    async def _check_spam(self, event: AstrMessageEvent, group_id: str, user_id: str) -> bool:
        spam_threshold = self.config.get("spam_threshold", 5)
        spam_time_window = self.config.get("spam_time_window", 10)
        
        current_time = time.time()
        key = f"{group_id}_{user_id}"
        
        self.spam_records[key] = [
            t for t in self.spam_records[key] 
            if current_time - t < spam_time_window
        ]
        
        self.spam_records[key].append(current_time)
        
        if len(self.spam_records[key]) >= spam_threshold:
            logger.warning(f"[群违规检测] [警告] 检测到刷屏！用户:{user_id} 在 {spam_time_window}秒内发送了 {len(self.spam_records[key])} 条消息")
            await self._handle_spam_violation(event, len(self.spam_records[key]), spam_time_window)
            self.spam_records[key] = []
            return True
        
        return False

    async def _check_profanity(self, event: AstrMessageEvent, group_id: str, user_id: str) -> bool:
        message_text = event.message_str
        
        if not message_text:
            return False
        
        profanity_use_ai = self.config.get("profanity_use_ai", True)
        
        if profanity_use_ai:
            api_endpoint = self.config.get("api_endpoint", "")
            api_key = self.config.get("api_key", "")
            
            if api_endpoint:
                logger.info(f"[群违规检测] 使用AI检测骂人内容...")
                try:
                    is_profanity, reason = await self._check_profanity_with_ai(api_endpoint, api_key, message_text)
                    if is_profanity:
                        logger.warning(f"[群违规检测] [警告] AI检测到骂人！用户:{user_id} 原因: {reason}")
                        await self._handle_profanity_violation(event, reason)
                        return True
                except Exception as e:
                    logger.error(f"[群违规检测] AI检测骂人失败: {e}")
                    return False
            else:
                logger.warning(f"[群违规检测] 未配置API，无法使用AI检测")
                return False
        else:
            profanity_keywords = self.config.get("profanity_keywords", [])
            
            if not profanity_keywords:
                return False
            
            message_text_lower = message_text.lower()
            
            for keyword in profanity_keywords:
                if keyword.lower() in message_text_lower:
                    logger.warning(f"[群违规检测] [警告] 检测到骂人！用户:{user_id} 消息包含关键词: {keyword}")
                    await self._handle_profanity_violation(event, f"包含关键词：{keyword}")
                    return True
        
        return False

    async def _check_profanity_with_ai(self, api_endpoint: str, api_key: str, message_text: str) -> tuple[bool, str]:
        model_name = self.config.get("model_name", "gpt-4o")
        
        prompt = """你是一个严格的内容审核助手。请判断以下文本是否包含骂人、侮辱、人身攻击等不当内容。

判断标准：
1. 包含脏话、粗口、侮辱性词汇
2. 对他人进行人身攻击
3. 使用侮辱性称呼
4. 含有恶意攻击性语言

请以严格的 JSON 格式回复（不要添加任何其他文字）：
{"is_profanity": true, "reason": "包含侮辱性词汇"}

注意：
- is_profanity 必须是 true 或 false（布尔值，不要加引号）
- reason 简要说明原因
- 宁可误判也不要漏判，保护群聊环境"""

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": f"{prompt}\n\n待检测文本：{message_text}"
                }
            ],
            "max_tokens": 200,
            "temperature": 0.1
        }

        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[群违规检测] AI检测骂人API请求失败: {response.status}")
                    return False, ""

                result = await response.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                logger.info(f"[群违规检测] AI骂人检测响应: {content}")

                try:
                    content = content.strip()
                    if content.startswith("```"):
                        lines = content.split('\n')
                        content = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])

                    json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
                    if json_match:
                        data = json.loads(json_match.group())
                    else:
                        data = json.loads(content)

                    is_profanity = data.get("is_profanity", False)
                    reason = data.get("reason", "")

                    return is_profanity, reason

                except json.JSONDecodeError as e:
                    logger.error(f"[群违规检测] 解析AI骂人检测结果失败: {e}")
                    return False

    async def _check_ad(self, event: AstrMessageEvent, group_id: str, user_id: str) -> bool:
        message_text = event.message_str
        
        if not message_text:
            return False
        
        ad_keywords = self.config.get("ad_keywords", [])
        
        if not ad_keywords:
            return False
        
        message_text_lower = message_text.lower()
        
        for keyword in ad_keywords:
            if keyword.lower() in message_text_lower:
                logger.warning(f"[群违规检测] [警告] 检测到广告！用户:{user_id} 消息包含关键词: {keyword}")
                await self._handle_ad_violation(event, keyword)
                return True
        
        return False

    async def _check_link(self, event: AstrMessageEvent, group_id: str, user_id: str) -> bool:
        message_text = event.message_str
        
        if not message_text:
            return False
        
        import re
        link_pattern = r'(https?://[^\s]+|www\.[^\s]+\.[^\s]+|[^\s]+\.(com|cn|net|org|io|xyz|top|vip|cc|me|tv|edu|gov)[^\s]*)'
        
        if re.search(link_pattern, message_text, re.IGNORECASE):
            logger.warning(f"[群违规检测] [警告] 检测到链接！用户:{user_id}")
            await self._handle_link_violation(event)
            return True
        
        return False

    async def _check_group_promotion(self, event: AstrMessageEvent, group_id: str, user_id: str) -> bool:
        message_text = event.message_str
        
        if not message_text:
            return False
        
        promotion_keywords = ["进群", "加群", "群号", "入群", "拉群", "建群"]
        
        has_keyword = any(keyword in message_text for keyword in promotion_keywords)
        
        if not has_keyword:
            return False
        
        import re
        group_pattern = r'[;；:,，\s]*(\d{5,12})'
        matches = re.findall(group_pattern, message_text)
        
        if matches:
            logger.warning(f"[群违规检测] [警告] 检测到群号推广！用户:{user_id} 消息: {message_text[:50]}")
            await self._handle_group_promotion_violation(event, matches)
            return True
        
        return False

    async def _handle_group_promotion_violation(self, event: AstrMessageEvent, group_numbers: list):
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        message_id = event.message_obj.message_id
        ban_duration = self.config.get("group_promotion_ban_duration", 600)
        notify = self.config.get("notify_on_violation", True)

        logger.info(f"[群违规检测] 群号推广违规 - 群:{group_id} 用户:{user_id} 消息ID:{message_id} 群号:{group_numbers}")
        
        self.violation_stats[user_id]["ad"] += 1

        await self._execute_ban(event, group_id, user_id, message_id, ban_duration, f"检测到群号推广行为，已撤回并禁言 {ban_duration} 秒。", notify)

    async def _handle_ad_violation(self, event: AstrMessageEvent, keyword: str):
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        message_id = event.message_obj.message_id
        ban_duration = self.config.get("ad_ban_duration", 600)
        notify = self.config.get("notify_on_violation", True)

        logger.info(f"[群违规检测] 广告违规 - 群:{group_id} 用户:{user_id} 消息ID:{message_id} 关键词:{keyword}")
        
        self.violation_stats[user_id]["ad"] += 1

        await self._execute_ban(event, group_id, user_id, message_id, ban_duration, f"检测到广告行为（关键词：{keyword}），已撤回并禁言 {ban_duration} 秒。", notify)

    async def _handle_link_violation(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        message_id = event.message_obj.message_id
        ban_duration = self.config.get("link_ban_duration", 600)
        notify = self.config.get("notify_on_violation", True)

        logger.info(f"[群违规检测] 链接违规 - 群:{group_id} 用户:{user_id} 消息ID:{message_id}")
        
        self.violation_stats[user_id]["link"] += 1

        await self._execute_ban(event, group_id, user_id, message_id, ban_duration, f"检测到链接，已撤回并禁言 {ban_duration} 秒。", notify)

    async def _execute_ban(self, event: AstrMessageEvent, group_id: str, user_id: str, message_id: str, ban_duration: int, notify_message: str, notify: bool):
        try:
            if not hasattr(event, 'bot'):
                logger.warning("[群违规检测] 无法获取平台客户端，event.bot 不存在")
                return

            bot = event.bot
            logger.info(f"[群违规检测] 获取到客户端: {type(bot)}")

            try:
                if hasattr(bot, 'api') and hasattr(bot.api, 'call_action'):
                    result = await bot.api.call_action('delete_msg', message_id=int(message_id))
                    logger.info(f"[群违规检测] [成功] 消息撤回成功")
                elif hasattr(bot, 'delete_msg'):
                    result = await bot.delete_msg(message_id=int(message_id))
                    logger.info(f"[群违规检测] [成功] 消息撤回成功")
                else:
                    logger.warning("[群违规检测] 当前平台不支持消息撤回")
                    return
            except Exception as e:
                logger.error(f"[群违规检测] 撤回消息失败: {e}")

            if hasattr(bot, 'api') and hasattr(bot.api, 'call_action'):
                try:
                    result = await bot.api.call_action(
                        'set_group_ban',
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=int(ban_duration)
                    )
                    logger.info(f"[群违规检测] [成功] 已禁言用户: {user_id} 时长: {ban_duration} 秒")
                except Exception as e:
                    logger.error(f"[群违规检测] 禁言用户失败: {e}")
            elif hasattr(bot, 'set_group_ban'):
                try:
                    result = await bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=int(ban_duration)
                    )
                    logger.info(f"[群违规检测] [成功] 已禁言用户: {user_id} 时长: {ban_duration} 秒")
                except Exception as e:
                    logger.error(f"[群违规检测] 禁言用户失败: {e}")
            else:
                logger.warning("[群违规检测] 当前平台不支持禁言操作")

            if notify:
                try:
                    from astrbot.api.event import MessageChain
                    chain = MessageChain().message(notify_message)
                    await self.context.send_message(event.unified_msg_origin, chain)
                    logger.info(f"[群违规检测] [成功] 已发送违规通知")
                except Exception as e:
                    logger.error(f"[群违规检测] 发送通知失败: {e}")

        except Exception as e:
            logger.error(f"[群违规检测] 执行禁言失败: {e}")

    async def _handle_profanity_violation(self, event: AstrMessageEvent, reason: str):
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        message_id = event.message_obj.message_id
        ban_duration = self.config.get("profanity_ban_duration", 600)
        notify = self.config.get("notify_on_violation", True)

        logger.info(f"[群违规检测] 骂人违规 - 群:{group_id} 用户:{user_id} 消息ID:{message_id} 原因:{reason}")

        try:
            if not hasattr(event, 'bot'):
                logger.warning("[群违规检测] 无法获取平台客户端，event.bot 不存在")
                return

            bot = event.bot
            logger.info(f"[群违规检测] 获取到客户端: {type(bot)}")

            try:
                if hasattr(bot, 'api') and hasattr(bot.api, 'call_action'):
                    logger.info(f"[群违规检测] 使用 bot.api.call_action 方法")
                    result = await bot.api.call_action('delete_msg', message_id=int(message_id))
                    logger.info(f"[群违规检测] API调用完成，返回结果: {result}")
                elif hasattr(bot, 'delete_msg'):
                    logger.info(f"[群违规检测] 使用 bot.delete_msg 方法")
                    result = await bot.delete_msg(message_id=int(message_id))
                    logger.info(f"[群违规检测] API调用完成，返回结果: {result}")
                else:
                    logger.warning("[群违规检测] 当前平台不支持消息撤回")
                    return
                    
                logger.info(f"[群违规检测] [成功] 消息撤回成功")
                        
            except Exception as e:
                logger.error(f"[群违规检测] 撤回消息失败: {e}")
                import traceback
                logger.error(traceback.format_exc())

            if hasattr(bot, 'api') and hasattr(bot.api, 'call_action'):
                try:
                    logger.info(f"[群违规检测] 尝试禁言用户，group_id={group_id}, user_id={user_id}, duration={ban_duration}秒")
                    result = await bot.api.call_action(
                        'set_group_ban',
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=int(ban_duration)
                    )
                    logger.info(f"[群违规检测] [成功] 已禁言用户: {user_id} 时长: {ban_duration} 秒")
                    logger.info(f"[群违规检测] 禁言API返回: {result}")
                except Exception as e:
                    logger.error(f"[群违规检测] 禁言用户失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            elif hasattr(bot, 'set_group_ban'):
                try:
                    logger.info(f"[群违规检测] 尝试禁言用户，group_id={group_id}, user_id={user_id}, duration={ban_duration}秒")
                    result = await bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=int(ban_duration)
                    )
                    logger.info(f"[群违规检测] [成功] 已禁言用户: {user_id} 时长: {ban_duration//60} 分钟")
                    logger.info(f"[群违规检测] 禁言API返回: {result}")
                except Exception as e:
                    logger.error(f"[群违规检测] 禁言用户失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            else:
                logger.warning("[群违规检测] 当前平台不支持禁言操作")

            if notify:
                try:
                    from astrbot.api.event import MessageChain
                    ban_minutes = ban_duration // 60
                    chain = MessageChain().message(f"检测到骂人行为，已撤回并禁言 {ban_duration} 秒。\n原因: {reason}")
                    await self.context.send_message(event.unified_msg_origin, chain)
                    logger.info(f"[群违规检测] [成功] 已发送违规通知")
                except Exception as e:
                    logger.error(f"[群违规检测] 发送通知失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

        except Exception as e:
            logger.error(f"[群违规检测] 处理骂人违规失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _handle_spam_violation(self, event: AstrMessageEvent, msg_count: int, time_window: int):
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        message_id = event.message_obj.message_id
        ban_duration = self.config.get("spam_ban_duration", 600)
        notify = self.config.get("notify_on_violation", True)

        logger.info(f"[群违规检测] 刷屏违规 - 群:{group_id} 用户:{user_id} 消息ID:{message_id}")

        try:
            if not hasattr(event, 'bot'):
                logger.warning("[群违规检测] 无法获取平台客户端，event.bot 不存在")
                return

            bot = event.bot
            logger.info(f"[群违规检测] 获取到客户端: {type(bot)}")

            try:
                if hasattr(bot, 'api') and hasattr(bot.api, 'call_action'):
                    logger.info(f"[群违规检测] 使用 bot.api.call_action 方法")
                    result = await bot.api.call_action('delete_msg', message_id=int(message_id))
                    logger.info(f"[群违规检测] API调用完成，返回结果: {result}")
                elif hasattr(bot, 'delete_msg'):
                    logger.info(f"[群违规检测] 使用 bot.delete_msg 方法")
                    result = await bot.delete_msg(message_id=int(message_id))
                    logger.info(f"[群违规检测] API调用完成，返回结果: {result}")
                else:
                    logger.warning("[群违规检测] 当前平台不支持消息撤回")
                    return
                    
                logger.info(f"[群违规检测] [成功] 消息撤回成功")
                        
            except Exception as e:
                logger.error(f"[群违规检测] 撤回消息失败: {e}")
                import traceback
                logger.error(traceback.format_exc())

            if hasattr(bot, 'api') and hasattr(bot.api, 'call_action'):
                try:
                    logger.info(f"[群违规检测] 尝试禁言用户，group_id={group_id}, user_id={user_id}, duration={ban_duration}秒")
                    result = await bot.api.call_action(
                        'set_group_ban',
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=int(ban_duration)
                    )
                    logger.info(f"[群违规检测] [成功] 已禁言用户: {user_id} 时长: {ban_duration//60} 分钟")
                    logger.info(f"[群违规检测] 禁言API返回: {result}")
                except Exception as e:
                    logger.error(f"[群违规检测] 禁言用户失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            elif hasattr(bot, 'set_group_ban'):
                try:
                    logger.info(f"[群违规检测] 尝试禁言用户，group_id={group_id}, user_id={user_id}, duration={ban_duration}秒")
                    result = await bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=int(ban_duration)
                    )
                    logger.info(f"[群违规检测] [成功] 已禁言用户: {user_id} 时长: {ban_duration//60} 分钟")
                    logger.info(f"[群违规检测] 禁言API返回: {result}")
                except Exception as e:
                    logger.error(f"[群违规检测] 禁言用户失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            else:
                logger.warning("[群违规检测] 当前平台不支持禁言操作")

            if notify:
                try:
                    from astrbot.api.event import MessageChain
                    ban_minutes = ban_duration // 60
                    chain = MessageChain().message(f"检测到刷屏行为（{time_window}秒内发送{msg_count}条消息），已撤回并禁言 {ban_duration} 秒。")
                    await self.context.send_message(event.unified_msg_origin, chain)
                    logger.info(f"[群违规检测] [成功] 已发送违规通知")
                except Exception as e:
                    logger.error(f"[群违规检测] 发送通知失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

        except Exception as e:
            logger.error(f"[群违规检测] 处理刷屏违规失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _handle_image_violation(self, event: AstrMessageEvent, reason: str):
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        message_id = event.message_obj.message_id
        ban_duration = self.config.get("ban_duration", 600)
        notify = self.config.get("notify_on_violation", True)

        logger.info(f"[群违规检测] 违规图片 - 群:{group_id} 用户:{user_id} 消息ID:{message_id} 原因:{reason}")

        try:
            if not hasattr(event, 'bot'):
                logger.warning("[群违规检测] 无法获取平台客户端，event.bot 不存在")
                return

            bot = event.bot
            logger.info(f"[群违规检测] 获取到客户端: {type(bot)}")

            try:
                if hasattr(bot, 'api') and hasattr(bot.api, 'call_action'):
                    logger.info(f"[群违规检测] 使用 bot.api.call_action 方法")
                    result = await bot.api.call_action('delete_msg', message_id=int(message_id))
                    logger.info(f"[群违规检测] API调用完成，返回结果: {result}")
                elif hasattr(bot, 'delete_msg'):
                    logger.info(f"[群违规检测] 使用 bot.delete_msg 方法")
                    result = await bot.delete_msg(message_id=int(message_id))
                    logger.info(f"[群违规检测] API调用完成，返回结果: {result}")
                else:
                    logger.warning("[群违规检测] 当前平台不支持消息撤回")
                    return
                    
                logger.info(f"[群违规检测] [成功] 消息撤回成功")
                        
            except Exception as e:
                logger.error(f"[群违规检测] 撤回消息失败: {e}")
                import traceback
                logger.error(traceback.format_exc())

            if hasattr(bot, 'api') and hasattr(bot.api, 'call_action'):
                try:
                    logger.info(f"[群违规检测] 尝试禁言用户，group_id={group_id}, user_id={user_id}, duration={ban_duration}秒")
                    result = await bot.api.call_action(
                        'set_group_ban',
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=int(ban_duration)
                    )
                    logger.info(f"[群违规检测] [成功] 已禁言用户: {user_id} 时长: {ban_duration//60} 分钟")
                    logger.info(f"[群违规检测] 禁言API返回: {result}")
                except Exception as e:
                    logger.error(f"[群违规检测] 禁言用户失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            elif hasattr(bot, 'set_group_ban'):
                try:
                    logger.info(f"[群违规检测] 尝试禁言用户，group_id={group_id}, user_id={user_id}, duration={ban_duration}秒")
                    result = await bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=int(ban_duration)
                    )
                    logger.info(f"[群违规检测] [成功] 已禁言用户: {user_id} 时长: {ban_duration//60} 分钟")
                    logger.info(f"[群违规检测] 禁言API返回: {result}")
                except Exception as e:
                    logger.error(f"[群违规检测] 禁言用户失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            else:
                logger.warning("[群违规检测] 当前平台不支持禁言操作")

            if notify:
                try:
                    from astrbot.api.event import MessageChain
                    ban_minutes = ban_duration // 60
                    chain = MessageChain().message(f"检测到违规图片，已撤回并禁言 {ban_duration} 秒。\n原因: {reason}")
                    await self.context.send_message(event.unified_msg_origin, chain)
                    logger.info(f"[群违规检测] [成功] 已发送违规通知")
                except Exception as e:
                    logger.error(f"[群违规检测] 发送通知失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

        except Exception as e:
            logger.error(f"[群违规检测] 处理违规失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

    @filter.command("群违规检测状态")
    async def status_command(self, event: AstrMessageEvent):
        api_type = self.config.get("api_type", "openai_vision")
        ban_minutes = self.config.get("ban_duration", 600)
        spam_ban_seconds = self.config.get("spam_ban_duration", 600)
        profanity_ban_seconds = self.config.get("profanity_ban_duration", 600)
        ad_ban_seconds = self.config.get("ad_ban_duration", 600)
        link_ban_seconds = self.config.get("link_ban_duration", 600)
        group_promotion_ban_seconds = self.config.get("group_promotion_ban_duration", 600)
        profanity_keywords = self.config.get("profanity_keywords", [])
        ad_keywords = self.config.get("ad_keywords", [])
        profanity_use_ai = self.config.get("profanity_use_ai", True)
        profanity_mode = "AI检测" if profanity_use_ai else "关键词检测"
        whitelist_users = self.config.get("whitelist_users", [])
        status_info = f"""群违规检测插件状态:
API类型: {api_type}
API站点: {self.config.get('api_endpoint', '未配置')}
API Key: {'已配置' if self.config.get('api_key') else '未配置'}
模型名称: {self.config.get('model_name', 'gpt-4o')}

【禁言时长】
图片违规: {ban_minutes} 秒
刷屏: {spam_ban_seconds} 秒
骂人: {profanity_ban_seconds} 秒
广告: {ad_ban_seconds} 秒
链接: {link_ban_seconds} 秒
群号推广: {group_promotion_ban_seconds} 秒

【检测功能】
图片检测: {'开启' if self.config.get('check_porn', True) or self.config.get('check_sexy', True) else '关闭'}
刷屏检测: {'开启' if self.config.get('spam_check_enabled', True) else '关闭'} (阈值: {self.config.get('spam_threshold', 5)} 条/{self.config.get('spam_time_window', 10)} 秒)
骂人检测: {'开启' if self.config.get('profanity_check_enabled', True) else '关闭'} (模式: {profanity_mode}, 关键词: {len(profanity_keywords)} 个)
广告检测: {'开启' if self.config.get('ad_check_enabled', True) else '关闭'} (关键词: {len(ad_keywords)} 个)
链接检测: {'开启' if self.config.get('link_check_enabled', False) else '关闭'}
群号推广检测: {'开启' if self.config.get('group_promotion_check_enabled', True) else '关闭'}

【其他设置】
监控群组: {self.config.get('enabled_groups', '全部') or '全部'}
白名单用户: {len(whitelist_users)} 人
管理员豁免: {'开启' if self.config.get('admin_bypass', True) else '关闭'}
违规通知: {'开启' if self.config.get('notify_on_violation', True) else '关闭'}

请在管理面板修改配置"""
        yield event.plain_result(status_info)

    @filter.command("设置图片禁言时长")
    async def set_image_ban_duration(self, event: AstrMessageEvent, seconds: int):
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        
        self.config["ban_duration"] = seconds
        yield event.plain_result(f"[成功] 图片违规禁言时长已设置为 {seconds} 秒")

    @filter.command("设置刷屏禁言时长")
    async def set_spam_ban_duration(self, event: AstrMessageEvent, seconds: int):
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        
        self.config["spam_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 刷屏禁言时长已设置为 {seconds} 秒")

    @filter.command("设置骂人禁言时长")
    async def set_profanity_ban_duration(self, event: AstrMessageEvent, seconds: int):
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        
        self.config["profanity_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 骂人禁言时长已设置为 {seconds} 秒")

    @filter.command("添加骂人关键词")
    async def add_profanity_keyword(self, event: AstrMessageEvent, keyword: str):
        profanity_keywords = self.config.get("profanity_keywords", [])
        
        if keyword in profanity_keywords:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 已存在")
            return
        
        profanity_keywords.append(keyword)
        self.config["profanity_keywords"] = profanity_keywords
        yield event.plain_result(f"[成功] 已添加关键词 '{keyword}'\n当前关键词数量: {len(profanity_keywords)}")

    @filter.command("删除骂人关键词")
    async def remove_profanity_keyword(self, event: AstrMessageEvent, keyword: str):
        profanity_keywords = self.config.get("profanity_keywords", [])
        
        if keyword not in profanity_keywords:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 不存在")
            return
        
        profanity_keywords.remove(keyword)
        self.config["profanity_keywords"] = profanity_keywords
        yield event.plain_result(f"[成功] 已删除关键词 '{keyword}'\n当前关键词数量: {len(profanity_keywords)}")

    @filter.command("查看骂人关键词")
    async def list_profanity_keywords(self, event: AstrMessageEvent):
        profanity_keywords = self.config.get("profanity_keywords", [])
        
        if not profanity_keywords:
            yield event.plain_result("当前没有设置骂人关键词")
            return
        
        keywords_list = "\n".join([f"{i+1}. {kw}" for i, kw in enumerate(profanity_keywords)])
        yield event.plain_result(f"当前骂人关键词列表 ({len(profanity_keywords)}个):\n{keywords_list}")

    @filter.command("切换骂人检测模式")
    async def toggle_profanity_mode(self, event: AstrMessageEvent):
        profanity_use_ai = self.config.get("profanity_use_ai", True)
        
        profanity_use_ai = not profanity_use_ai
        self.config["profanity_use_ai"] = profanity_use_ai
        
        mode = "AI检测" if profanity_use_ai else "关键词检测"
        yield event.plain_result(f"[成功] 已切换为 {mode} 模式")

    @filter.command("添加白名单用户")
    async def add_whitelist_user(self, event: AstrMessageEvent, user_id: str):
        whitelist_users = self.config.get("whitelist_users", [])
        
        if str(user_id) in [str(u) for u in whitelist_users]:
            yield event.plain_result(f"[错误] 用户 {user_id} 已在白名单中")
            return
        
        whitelist_users.append(user_id)
        self.config["whitelist_users"] = whitelist_users
        yield event.plain_result(f"[成功] 已添加用户 {user_id} 到白名单\n当前白名单人数: {len(whitelist_users)}")

    @filter.command("删除白名单用户")
    async def remove_whitelist_user(self, event: AstrMessageEvent, user_id: str):
        whitelist_users = self.config.get("whitelist_users", [])
        
        if str(user_id) not in [str(u) for u in whitelist_users]:
            yield event.plain_result(f"[错误] 用户 {user_id} 不在白名单中")
            return
        
        whitelist_users = [u for u in whitelist_users if str(u) != str(user_id)]
        self.config["whitelist_users"] = whitelist_users
        yield event.plain_result(f"[成功] 已从白名单移除用户 {user_id}\n当前白名单人数: {len(whitelist_users)}")

    @filter.command("查看白名单")
    async def list_whitelist(self, event: AstrMessageEvent):
        whitelist_users = self.config.get("whitelist_users", [])
        
        if not whitelist_users:
            yield event.plain_result("当前白名单为空")
            return
        
        users_list = "\n".join([f"{i+1}. {uid}" for i, uid in enumerate(whitelist_users)])
        yield event.plain_result(f"当前白名单用户 ({len(whitelist_users)}人):\n{users_list}")

    @filter.command("查看违规统计")
    async def view_violation_stats(self, event: AstrMessageEvent, user_id: str = ""):
        if user_id:
            stats = self.violation_stats.get(user_id, {"image": 0, "spam": 0, "profanity": 0, "ad": 0, "link": 0})
            total = sum(stats.values())
            stats_info = f"""用户 {user_id} 违规统计:
图片违规: {stats['image']} 次
刷屏: {stats['spam']} 次
骂人: {stats['profanity']} 次
广告: {stats['ad']} 次
链接: {stats['link']} 次
总计: {total} 次"""
            yield event.plain_result(stats_info)
        else:
            total_users = len(self.violation_stats)
            if total_users == 0:
                yield event.plain_result("暂无违规记录")
                return
            
            total_violations = sum(sum(stats.values()) for stats in self.violation_stats.values())
            yield event.plain_result(f"违规统计概览:\n违规用户数: {total_users} 人\n总违规次数: {total_violations} 次")

    @filter.command("设置广告禁言时长")
    async def set_ad_ban_duration(self, event: AstrMessageEvent, seconds: int):
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        
        self.config["ad_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 广告禁言时长已设置为 {seconds} 秒")

    @filter.command("设置链接禁言时长")
    async def set_link_ban_duration(self, event: AstrMessageEvent, seconds: int):
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        
        self.config["link_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 链接禁言时长已设置为 {seconds} 秒")

    @filter.command("添加广告关键词")
    async def add_ad_keyword(self, event: AstrMessageEvent, keyword: str):
        ad_keywords = self.config.get("ad_keywords", [])
        
        if keyword in ad_keywords:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 已存在")
            return
        
        ad_keywords.append(keyword)
        self.config["ad_keywords"] = ad_keywords
        yield event.plain_result(f"[成功] 已添加广告关键词 '{keyword}'\n当前关键词数量: {len(ad_keywords)}")

    @filter.command("删除广告关键词")
    async def remove_ad_keyword(self, event: AstrMessageEvent, keyword: str):
        ad_keywords = self.config.get("ad_keywords", [])
        
        if keyword not in ad_keywords:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 不存在")
            return
        
        ad_keywords.remove(keyword)
        self.config["ad_keywords"] = ad_keywords
        yield event.plain_result(f"[成功] 已删除广告关键词 '{keyword}'\n当前关键词数量: {len(ad_keywords)}")

    @filter.command("查看广告关键词")
    async def list_ad_keywords(self, event: AstrMessageEvent):
        ad_keywords = self.config.get("ad_keywords", [])
        
        if not ad_keywords:
            yield event.plain_result("当前没有设置广告关键词")
            return
        
        keywords_list = "\n".join([f"{i+1}. {kw}" for i, kw in enumerate(ad_keywords[:20])])
        more = f"\n... 还有 {len(ad_keywords) - 20} 个" if len(ad_keywords) > 20 else ""
        yield event.plain_result(f"当前广告关键词列表 ({len(ad_keywords)}个):\n{keywords_list}{more}")

    @filter.command("设置群号推广禁言时长")
    async def set_group_promotion_ban_duration(self, event: AstrMessageEvent, seconds: int):
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        
        self.config["group_promotion_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 群号推广禁言时长已设置为 {seconds} 秒")

    async def terminate(self):
        logger.info("[群违规检测] 插件已卸载")

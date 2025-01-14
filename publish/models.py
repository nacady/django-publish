from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.db.models.base import ModelBase
from django.db.models.fields.related import RelatedField
from django.db.models.query import QuerySet, Q

from .signals import pre_publish, post_publish
from .utils import NestedSet


# this takes some inspiration from the publisher stuff in
# django-cms 2.0
# e.g. http://github.com/digi604/django-cms-2.0/blob/master/publisher/models.py
#
# but we want this to be a reusable/standalone app and have a few different needs
#

try:
    stringtype = basestring
except NameError:  # Python 3, basestring causes NameError
    stringtype = str


class PublishException(Exception):
    pass


class UnpublishException(Exception):
    pass


class PublishableQuerySet(QuerySet):
    def changed(self):
        '''all draft objects that have not been published yet'''
        return self.filter(Publishable.Q_CHANGED)

    def deleted(self):
        '''public objects that need deleting'''
        return self.filter(Publishable.Q_DELETED)

    def draft(self):
        '''all draft objects'''
        return self.filter(Publishable.Q_DRAFT)

    def draft_and_deleted(self):
        return self.filter(Publishable.Q_DRAFT | Publishable.Q_DELETED)

    def published(self):
        '''all public/published objects'''
        return self.filter(Publishable.Q_PUBLISHED)

    def publish(self, all_published=None):
        '''publish all models in this queryset'''
        if all_published is None:
            all_published = NestedSet()
        for p in self:
            p.publish(all_published=all_published)

    def delete(self, mark_for_deletion=True):
        '''
        override delete so that we call delete on each object separately, as delete needs
        to set some flags etc
        '''
        for p in self:
            p.delete(mark_for_deletion=mark_for_deletion)


class PublishableManager(models.Manager):
    def get_queryset(self):
        return PublishableQuerySet(self.model)

    def get_query_set(self):
        return PublishableQuerySet(self.model)

    def changed(self):
        '''all draft objects that have not been published yet'''
        return self.get_query_set().changed()

    def deleted(self):
        '''public objects that need deleting'''
        return self.get_query_set().deleted()

    def draft(self):
        '''all draft objects'''
        return self.get_query_set().draft()

    def draft_and_deleted(self):
        return self.get_query_set().draft_and_deleted()

    def published(self):
        '''all public/published objects'''
        return self.get_query_set().published()


class PublishableBase(ModelBase):
    def __new__(cls, name, bases, attrs):
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        new_class = super(PublishableBase, cls).__new__(cls, name, bases, attrs)
        if new_class._meta.abstract:
            return new_class

        # insert an extra permission in for "Can publish"
        # as well as a "method" to find name of publish_permission for this object
        opts = new_class._meta
        name = u'Can publish %s' % opts.verbose_name
        code = u'publish_%s' % opts.object_name.lower()
        try:
            content_type = ContentType.objects.get_for_model(new_class)
        except:
            return new_class

        try:
            permission = Permission.objects.get_or_create(
                codename=code,
                name=name,
                content_type=content_type,
            )
        except:
          pass
        return new_class


class Publishable(models.Model, metaclass=PublishableBase):
    PUBLISH_DEFAULT = 0
    PUBLISH_CHANGED = 1
    PUBLISH_DELETE = 2

    PUBLISH_CHOICES = ((PUBLISH_DEFAULT, 'Published'), (PUBLISH_CHANGED, 'Changed'), (PUBLISH_DELETE, 'To be deleted'))

    # make these available here so can easily re-use them in other code
    Q_PUBLISHED = Q(is_public=True)
    Q_DRAFT = Q(is_public=False) & ~Q(publish_state=PUBLISH_DELETE)
    Q_CHANGED = Q(is_public=False, publish_state=PUBLISH_CHANGED)
    Q_DELETED = Q(is_public=False, publish_state=PUBLISH_DELETE)

    is_public = models.BooleanField(default=False, editable=False, db_index=True)
    publish_state = models.IntegerField('Publication status', editable=False, db_index=True, choices=PUBLISH_CHOICES,
                                        default=PUBLISH_CHANGED)
    public = models.OneToOneField('self', related_name='draft', null=True,
                                  editable=False, on_delete=models.SET_NULL)

    class Meta:
        abstract = True

    class PublishMeta(object):
        publish_exclude_fields = ['id', 'is_public', 'publish_state', 'public', 'draft']
        publish_reverse_fields = []
        publish_functions = {}

        @classmethod
        def _combined_fields(cls, field_name):
            fields = []
            for clazz in cls.__mro__:
                fields.extend(getattr(clazz, field_name, []))
            return fields

        @classmethod
        def excluded_fields(cls):
            return cls._combined_fields('publish_exclude_fields')

        @classmethod
        def reverse_fields_to_publish(cls):
            return cls._combined_fields('publish_reverse_fields')

        @classmethod
        def find_publish_function(cls, field_name, default_function):
            '''
                Search to see if there is a function to copy the given field over.
                Function should take same params as setattr()
            '''
            for clazz in cls.__mro__:
                publish_functions = getattr(clazz, 'publish_functions', {})
                fn = publish_functions.get(field_name, None)
                if fn:
                    return fn
            return default_function

    objects = PublishableManager()

    def is_marked_for_deletion(self):
        return self.publish_state == Publishable.PUBLISH_DELETE

    def get_public_absolute_url(self):
        if self.public:
            get_absolute_url = getattr(self.public, 'get_absolute_url', None)
            if get_absolute_url:
                return get_absolute_url()
        return None

    def save(self, mark_changed=True, *arg, **kw):
        if not self.is_public and mark_changed:
            if self.publish_state == Publishable.PUBLISH_DELETE:
                raise PublishException("Attempting to save model marked for deletion")
            self.publish_state = Publishable.PUBLISH_CHANGED

        super(Publishable, self).save(*arg, **kw)

    def delete(self, mark_for_deletion=True):
        if self.public and mark_for_deletion:
            self.publish_state = Publishable.PUBLISH_DELETE
            self.save(mark_changed=False)
        else:
            super(Publishable, self).delete()

    def undelete(self):
        self.publish_state = Publishable.PUBLISH_CHANGED
        self.save(mark_changed=False)

    def _pre_publish(self, dry_run, all_published, deleted=False):
        if not dry_run:
            sender = self.__class__
            pre_publish.send(sender=sender, instance=self, deleted=deleted)

    def _post_publish(self, dry_run, all_published, deleted=False):
        if not dry_run:
            # we need to make sure we get the instance that actually
            # got published (in case it was indirectly published elsewhere)
            sender = self.__class__
            instance = all_published.original(self)
            post_publish.send(sender=sender, instance=instance, deleted=deleted)

    def publish(self, dry_run=False, all_published=None, parent=None):
        '''
        either publish changes or deletions, depending on
        whether this model is public or draft.

        public models will be examined to see if they need deleting
        and deleted if so.
        '''
        if self.is_public:
            raise PublishException("Cannot publish public model - publish should be called from draft model")
        if self.pk is None:
            raise PublishException("Please save model before publishing")

        if self.publish_state == Publishable.PUBLISH_DELETE:
            self.publish_deletions(dry_run=dry_run, all_published=all_published, parent=parent)
            return None
        else:
            return self.publish_changes(dry_run=dry_run, all_published=all_published, parent=parent)

    def unpublish(self, dry_run=False):
        '''
        unpublish models by deleting public model
        '''
        if self.is_public:
            raise UnpublishException("Cannot unpublish a public model - unpublish should be called from draft model")
        if self.pk is None:
            raise UnpublishException("Please save the model before unpublishing")

        public_model = self.public

        if public_model and not dry_run:
            self.public = None
            self.save()
            public_model.delete(mark_for_deletion=False)
        return public_model

    def _get_public_or_publish(self, *arg, **kw):
        # only publish if we don't yet have an id for the
        # public model
        if self.public:
            return self.public
        return self.publish(*arg, **kw)

    def _get_through_model(self, field_object):
        '''
        Get the "through" model associated with this field.
        Need to handle things differently for Django1.1 vs Django1.2
        In 1.1 through is a string and through_model has class
        In 1.2 through is the class
        '''
        through = field_object.remote_field.through
        if through:
            if isinstance(through, stringtype):
                return field_object.remote_field.through_model
            return through
        return None

    def _changes_need_publishing(self):
        return True

    def _get_all_related_objects(self):
        # The following mimics the deprecated Options.get_all_related_objects
        return [
            f for f in self._meta.get_fields()
            if (f.one_to_many or f.one_to_one)
               and f.auto_created and not f.concrete
        ]

    def publish_changes(self, dry_run=False, all_published=None, parent=None):
        '''
        publish changes to the model - basically copy all of it's content to another copy in the
        database.
        if you set dry_run=True nothing will be written to the database.  combined with
        the all_published value one can therefore get information about what other models
        would be affected by this function
        '''

        assert not self.is_public, "Cannot publish public model - publish should be called from draft model"
        assert self.pk is not None, "Please save model before publishing"

        # avoid mutual recursion
        if all_published is None:
            all_published = NestedSet()

        if self in all_published:
            return all_published.original(self).public

        all_published.add(self, parent=parent)

        self._pre_publish(dry_run, all_published)

        public_version = self.public
        if not public_version:
            public_version = self.__class__(is_public=True)

        excluded_fields = self.PublishMeta.excluded_fields()
        reverse_fields_to_publish = self.PublishMeta.reverse_fields_to_publish()

        if self._changes_need_publishing():
            # copy over regular fields
            for field in self._meta.fields:
                if field.name in excluded_fields:
                    continue

                value = getattr(self, field.name)
                if isinstance(field, RelatedField):
                    related = field.remote_field.model
                    if issubclass(related, Publishable):
                        if value is not None:
                            value = value._get_public_or_publish(dry_run=dry_run, all_published=all_published,
                                                                 parent=self)

                if not dry_run:
                    publish_function = self.PublishMeta.find_publish_function(field.name, setattr)
                    publish_function(public_version, field.name, value)

            # save the public version and update
            # state so we know everything is up-to-date
            if not dry_run:
                public_version.save()
                self.public = public_version
                self.publish_state = Publishable.PUBLISH_DEFAULT
                self.save(mark_changed=False)

        # copy over many-to-many fields
        for field in self._meta.many_to_many:
            name = field.name
            if name in excluded_fields:
                continue

            m2m_manager = getattr(self, name)
            public_objs = list(m2m_manager.all())

            field_object = self._meta.get_field(name)
            through_model = self._get_through_model(field_object)
            if through_model:
                # see if we can work out which reverse relationship this is
                # see if we are using our own "through" table or not
                if issubclass(through_model, Publishable):
                    # this will be db name (e.g. with _id on end)
                    m2m_reverse_name = field_object.m2m_reverse_name()
                    for reverse_field in through_model._meta.fields:
                        if reverse_field.column == m2m_reverse_name:
                            related_name = reverse_field.name
                            related_field = getattr(through_model, related_name).field
                            reverse_name = related_field.remote_field.get_accessor_name()
                            reverse_fields_to_publish.append(reverse_name)
                            break
                    continue  # m2m via through table won't be dealt with here

            related = field_object.remote_field.model
            if issubclass(related, Publishable):
                public_objs = [p._get_public_or_publish(dry_run=dry_run, all_published=all_published, parent=self) for p
                               in public_objs]

            if not dry_run:
                public_m2m_manager = getattr(public_version, name)

                old_objs = public_m2m_manager.all()
                public_m2m_manager.remove(*old_objs)
                public_m2m_manager.add(*public_objs)

        related_objects = self._get_all_related_objects()
        # one-to-many and one-to-one reverse relations
        for obj in related_objects:
            if issubclass(obj.model, Publishable):
                name = obj.get_accessor_name()
                if name in excluded_fields:
                    continue
                if name not in reverse_fields_to_publish:
                    continue
                if obj.field.remote_field.multiple:
                    related_items = getattr(self, name).all()
                else:
                    try:
                        related_items = [getattr(self, name)]
                    except (obj.model.DoesNotExist, ObjectDoesNotExist):
                        related_items = []

                for related_item in related_items:
                    related_item.publish(dry_run=dry_run, all_published=all_published, parent=self)

                # make sure we tidy up anything that needs deleting
                if self.public and not dry_run:
                    if obj.field.remote_field.multiple:
                        public_ids = [r.public_id for r in related_items]
                        deleted_items = getattr(self.public, name).exclude(pk__in=public_ids)
                        deleted_items.delete(mark_for_deletion=False)

        self._post_publish(dry_run, all_published)

        return public_version

    def publish_deletions(self, all_published=None, parent=None, dry_run=False):
        '''
        actually delete models that have been marked for deletion
        '''
        if self.publish_state != Publishable.PUBLISH_DELETE:
            return

        if all_published is None:
            all_published = NestedSet()

        if self in all_published:
            return

        all_published.add(self, parent=parent)

        self._pre_publish(dry_run, all_published, deleted=True)

        related_objects = self._get_all_related_objects()
        for related in related_objects:
            if not issubclass(related.model, Publishable):
                continue
            name = related.get_accessor_name()
            if name in self.PublishMeta.excluded_fields():
                continue
            try:
                instances = getattr(self, name).all()
            except AttributeError:
                instances = [getattr(self, name)]
            for instance in instances:
                instance.publish_deletions(all_published=all_published, parent=self, dry_run=dry_run)

        if not dry_run:
            public = self.public
            self.delete(mark_for_deletion=False)
            if public:
                public.delete(mark_for_deletion=False)

        self._post_publish(dry_run, all_published, deleted=True)
